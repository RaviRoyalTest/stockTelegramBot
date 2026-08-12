"""Market-hours windows for scheduled reports (pure logic, no I/O).

Automatic reports can be gated to run only while a market is open so scans
land during live trading instead of firing around the clock. Two markets are
built in:

  * 'in' - India (NSE/BSE): 09:15-15:30 Asia/Kolkata, Mon-Fri
  * 'us' - US (NASDAQ/NYSE): 09:30-16:00 America/New_York, Mon-Fri

'any' (or 'off') disables gating so an entry runs whenever its timer fires.
The helpers in this module are pure - no network, no storage - and take an
entry dict + a default market so the bot, the scheduler and the web dashboard
all share exactly the same rules.
"""
from __future__ import annotations

import datetime as _datetime
import re

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - tzdata unavailable
    ZoneInfo = None

MARKETS = {
    "in": {
        "label": "India (NSE/BSE)",
        "tz": "Asia/Kolkata",
        "open": "09:15",
        "close": "15:30",
    },
    "us": {
        "label": "US (NASDAQ/NYSE)",
        "tz": "America/New_York",
        "open": "09:30",
        "close": "16:00",
    },
}

MARKET_KEYS = tuple(MARKETS.keys())
ANY_WORDS = ("any", "off", "none", "always", "24x7", "anytime")

_HHMM_RE = re.compile(r"(\d{1,2}):(\d{2})")


def normalise_market(market) -> str:
    """Normalise a market word to one of 'in' / 'us' / 'any' (unknown -> 'in')."""
    value = str(market or "").strip().lower()
    if value in ANY_WORDS:
        return "any"
    if value in ("ind", "india", "nse", "bse", "ist", "in"):
        return "in"
    if value in ("usa", "american", "nyse", "nasdaq", "et", "est", "edt", "us"):
        return "us"
    return "in"


def _tz_abbrev(tz_name: str) -> str:
    if tz_name == "Asia/Kolkata":
        return "IST"
    if tz_name == "America/New_York":
        return "ET"
    return tz_name


def market_label(market) -> str:
    """Human label for a market gate, e.g. 'India (NSE/BSE) · 09:15–15:30 IST'."""
    key = normalise_market(market)
    info = MARKETS.get(key)
    if not info:
        return "any time"
    return f"{info['label']} \u00b7 {info['open']}\u2013{info['close']} {_tz_abbrev(info['tz'])}"


def market_timezone(market):
    """zoneinfo.ZoneInfo for a market (None when unavailable)."""
    info = MARKETS.get(normalise_market(market))
    if not info or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(info["tz"])
    except Exception:
        return None


def _now_ts(now=None) -> float:
    """Normalise 'now' (None / epoch float / datetime) to an epoch timestamp."""
    if now is None:
        return _datetime.datetime.now(_datetime.timezone.utc).timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    try:
        return now.timestamp()
    except AttributeError:
        return _datetime.datetime.now(_datetime.timezone.utc).timestamp()


def local_now(market, now=None) -> _datetime.datetime:
    """Current wall-clock time in the market's timezone (host-local fallback).

    `now` may be None (use the clock), an epoch timestamp or a datetime.
    """
    if now is None:
        now_utc = _datetime.datetime.now(_datetime.timezone.utc)
    elif isinstance(now, (int, float)):
        now_utc = _datetime.datetime.fromtimestamp(now, _datetime.timezone.utc)
    else:
        now_utc = now
    timezone = market_timezone(market)
    if timezone is None:
        return now_utc.astimezone()
    return now_utc.astimezone(timezone)


def _hhmm_minutes(hhmm) -> int | None:
    match = _HHMM_RE.fullmatch(str(hhmm or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def is_between(open_hm: str, close_hm: str, market="in", now=None) -> bool:
    """True when 'now' (default: the current moment) is inside open..close.

    Supports overnight windows (e.g. 22:00-06:00). Invalid times fall back to
    'always allowed' so a malformed window never silently suppresses a report.
    """
    local = local_now(market, now)
    current_minutes = local.hour * 60 + local.minute
    open_minutes = _hhmm_minutes(open_hm)
    close_minutes = _hhmm_minutes(close_hm)
    if open_minutes is None or close_minutes is None:
        return True
    if open_minutes <= close_minutes:
        return open_minutes <= current_minutes < close_minutes
    return current_minutes >= open_minutes or current_minutes < close_minutes


def is_market_open(market, now=None) -> bool:
    """True when 'now' falls inside the market's regular Mon-Fri session."""
    key = normalise_market(market)
    if key == "any":
        return True
    info = MARKETS.get(key)
    if not info:
        return True
    local = local_now(key, now)
    if local.weekday() >= 5:  # Sat / Sun
        return False
    return is_between(info["open"], info["close"], key, now)


def next_open_after(market, after_ts=None) -> float:
    """Epoch seconds when the market next opens (used for pause resume labels)."""
    info = MARKETS.get(normalise_market(market))
    if not info:
        return float(after_ts or 0)
    timezone = market_timezone(market)
    if after_ts is not None:
        base = _datetime.datetime.fromtimestamp(after_ts, timezone)
    else:
        base = local_now(market)
    open_hour, open_minute = (int(part) for part in info["open"].split(":"))
    day = base.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(14):  # at most two calendar weeks ahead
        if day.weekday() < 5:
            candidate = day.replace(hour=open_hour, minute=open_minute)
            if candidate > base:
                return candidate.timestamp()
        day = day + _datetime.timedelta(days=1)
    return float((base + _datetime.timedelta(days=14)).timestamp())


def entry_market(entry: dict, default="in") -> str:
    """The market gate of a schedule entry ('in' / 'us' / 'any')."""
    return normalise_market((entry or {}).get("market") or default)


def entry_paused(entry: dict, now=None) -> bool:
    """True while the entry's paused_until (ISO) is still in the future."""
    raw = (entry or {}).get("paused_until")
    if not raw:
        return False
    now_ts = _now_ts(now)
    try:
        return now_ts < _datetime.datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return False


def entry_paused_until(entry: dict) -> str:
    """Human 'until' text for a paused entry ('' when not paused)."""
    raw = (entry or {}).get("paused_until")
    if not raw:
        return ""
    try:
        return _datetime.datetime.fromisoformat(str(raw)).strftime("%d-%b %H:%M")
    except (TypeError, ValueError):
        return ""


def entry_in_window(entry: dict, default="in", now=None) -> bool:
    """True when 'now' is inside the entry's allowed run window.

    An explicit window_start/window_end wins over market hours. Without an
    explicit window the market's regular hours are used (any = always open).
    """
    window_start = (entry or {}).get("window_start")
    window_end = (entry or {}).get("window_end")
    if window_start and window_end:
        return is_between(window_start, window_end, entry_market(entry, default), now)
    return is_market_open(entry_market(entry, default), now)
