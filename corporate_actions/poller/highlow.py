"""52-week high / low alert helpers: who watches what, and what counts.

The Poller engine (engine.py) owns the scan thread, the quote fetch and the
Telegram sends - this module answers three questions only:

  * which chats want 52-week alerts and which stocks they cover
  * did a stock make a NEW 52-week high/low today (candidate_kind +
    is_fresh_extreme)
  * what is the per-chat-per-day dedup key (alert_key)

A "reach" is deliberately strict: today's session high must be ABOVE the
highest high of the previous 52 weeks (mirrored for lows). A stock that
merely sits at, or re-tests, a high set weeks ago does not alert - so an
uptrend parked at its high pings once on the breakout day, not every cycle
and not every following day.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .. import storage
from ..core.dates import IST, today_ist
from . import watcher as watcher_module

log = logging.getLogger(__name__)

# Same default shape as the sudden-move watcher: ON out of the box over
# NIFTY 100, so a chat that never configured anything still gets the alerts.
DEFAULT_HIGHLOW = {"enabled": True, "universe": "nifty100"}

# A quote only becomes a candidate when the session's extreme is this close
# to the quoted 52-week level. Exchange 52-week fields already include
# today's high/low, so a genuine break lands exactly on them; the tolerance
# only absorbs rounding and a slightly stale cached level.
TOUCH_TOLERANCE_PCT = 0.1

# Exactly 52 weeks, matching the trailing window the exchange (and Yahoo)
# uses for the quoted 52-week extremes. A wider window would silently
# suppress a real breakout of the exchange's 52-week level.
LOOKBACK_DAYS = 365


# ------------------------------------------------------------------ settings
def highlow_settings(chat_id) -> dict:
    """The chat's effective 52-week alert config (defaults when never set)."""
    stored = storage.get_user_settings(chat_id).get("highlow")
    if not stored:
        return dict(DEFAULT_HIGHLOW)
    return stored


def highlow_targets() -> list[tuple[str, dict]]:
    """[(chat_id, config)] for every chat with 52-week alerts switched on."""
    return [
        (str(chat_id), highlow_settings(chat_id))
        for chat_id in storage.load_settings()
        if highlow_settings(chat_id).get("enabled")
    ]


def highlow_symbols(chat_id, universe) -> list[str]:
    """Symbols to scan for a chat: nifty100 / nifty500 / mylist.

    Reuses the watcher's universe resolver so both alert types cover exactly
    the same stocks for a given universe token.
    """
    return watcher_module.watcher_symbols(chat_id, universe)


def alert_key(chat_id, today: date, symbol: str, kind: str) -> str:
    """Dedup key: one 52-week high and one 52-week low alert per stock per day."""
    return f"hilo|{chat_id}|{today.isoformat()}|{symbol.upper()}|{kind}"


# ----------------------------------------------------------------- detection
def _num(value) -> float | None:
    """Float from a possibly-stringy quote field (None when unusable)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def candidate_kind(quote: dict) -> str | None:
    """'high' / 'low' when the session traded at or through the 52-week level.

    Cheap pre-filter that runs on every scanned symbol: the day's high/low
    against the quote's own 52-week extremes. Verification that the extreme
    is genuinely NEW happens later in is_fresh_extreme, and only for the few
    symbols that get this far.
    """
    if not quote:
        return None
    price = _num(quote.get("price"))
    if price is None:
        return None
    day_high = _num(quote.get("high")) or price
    day_low = _num(quote.get("low")) or price
    high_52 = _num(quote.get("wk52_high"))
    low_52 = _num(quote.get("wk52_low"))
    tolerance = TOUCH_TOLERANCE_PCT / 100.0
    if high_52 and high_52 > 0 and day_high >= high_52 * (1 - tolerance):
        return "high"
    if low_52 and low_52 > 0 and day_low <= low_52 * (1 + tolerance):
        return "low"
    return None


def _bar_date(timestamp) -> date | None:
    """Calendar date (IST) of a Yahoo bar timestamp."""
    try:
        moment = datetime.fromtimestamp(float(timestamp), tz=IST) if IST else datetime.fromtimestamp(float(timestamp))
    except (TypeError, ValueError, OSError):
        return None
    return moment.date()


def prior_extremes(bars: dict, today: date) -> tuple[float | None, float | None]:
    """(highest high, lowest low) of the completed sessions in the last year.

    Today's own bar is excluded on purpose - it is the session being judged,
    so including it would make every candidate look like a new extreme.
    """
    if not bars:
        return None, None
    start = today - timedelta(days=LOOKBACK_DAYS)
    highest = lowest = None
    for timestamp, high, low in zip(
        bars.get("timestamp") or [], bars.get("high") or [], bars.get("low") or []
    ):
        bar_day = _bar_date(timestamp)
        if bar_day is None or bar_day >= today or bar_day < start:
            continue
        high_value = _num(high)
        low_value = _num(low)
        if high_value is not None and (highest is None or high_value > highest):
            highest = high_value
        if low_value is not None and (lowest is None or low_value < lowest):
            lowest = low_value
    return highest, lowest


def is_fresh_extreme(
    exchange: str,
    symbol: str,
    kind: str,
    quote: dict,
    today: date | None = None,
    bars: dict | None = None,
) -> bool:
    """True when today's session cleared the PREVIOUS 52 weeks' extreme.

    Fails OPEN when the daily bars cannot be fetched: a real breakout must
    not be lost because the extra confirmation request hiccupped, and the
    per-day dedup key still keeps it to one alert.
    """
    today = today or today_ist()
    if kind == "high":
        session_extreme = _num(quote.get("high")) or _num(quote.get("price"))
    else:
        session_extreme = _num(quote.get("low")) or _num(quote.get("price"))
    if session_extreme is None:
        return False
    if bars is None:
        from ..sources.ohlc import get_ohlc

        try:
            bars = get_ohlc(exchange or "NSE", symbol, "1d")
        except Exception as error:  # never let a quote scan die on one fetch
            log.info("52w bars fetch failed for %s:%s - %s", exchange, symbol, error)
            bars = None
    prior_high, prior_low = prior_extremes(bars, today)
    if kind == "high":
        return True if prior_high is None else session_extreme > prior_high
    return True if prior_low is None else session_extreme < prior_low


def fresh_breakouts(quotes: dict[str, dict], today: date | None = None) -> dict[str, str]:
    """{SYMBOL: 'high'|'low'} for stocks that made a NEW 52-week extreme today.

    Runs once per scan cycle, so a symbol shared by several chats is verified
    with at most one extra bars request.
    """
    today = today or today_ist()
    breakouts: dict[str, str] = {}
    for symbol, quote in (quotes or {}).items():
        kind = candidate_kind(quote or {})
        if not kind:
            continue
        if is_fresh_extreme(quote.get("exchange") or "NSE", symbol, kind, quote, today):
            breakouts[symbol.upper()] = kind
    return breakouts


def alert_details(symbol: str) -> dict:
    """Fundamentals snapshot for an alert message (best effort, cached).

    Called only when an alert really fires, so the deep fetch (Yahoo
    quoteSummary + chart + screener.in ratio block) never touches the scan
    loop's per-symbol cost. A failure returns {} and the alert still sends
    with the quote-only fields.
    """
    try:
        from ..sources.fundamentals import get_fundamentals

        return get_fundamentals(symbol, with_screener=True) or {}
    except Exception as error:  # enrichment must never cost us the alert
        log.info("52w alert details unavailable for %s: %s", symbol, error)
        return {}


def pending_alerts(
    targets: list[tuple[str, dict]],
    quotes: dict[str, dict],
    breakouts: dict[str, str],
    seen: set,
    today: date,
) -> list[tuple[str, str, dict, str]]:
    """Alerts to send: (chat_id, symbol, quote, kind) not yet sent today."""
    out: list[tuple[str, str, dict, str]] = []
    for chat_id, config_ in targets:
        for symbol in highlow_symbols(chat_id, config_.get("universe", "nifty100")):
            key_symbol = symbol.upper()
            kind = breakouts.get(key_symbol)
            quote = (quotes or {}).get(key_symbol)
            if not kind or not quote:
                continue
            if alert_key(chat_id, today, key_symbol, kind) in seen:
                continue
            out.append((chat_id, symbol, quote, kind))
    return out
