"""Date/time helpers used by the poller, bot and dashboard.

Everything here is pure - no network, no storage, no Telegram. The host often
runs on UTC where the calendar flips at 18:30 IST, so all "today" logic must
follow the market's calendar (Asia/Kolkata), not the host's local date.
"""
from __future__ import annotations

import datetime as _dt
import re
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata unavailable - fall back to host local time
    IST = None


def today_ist() -> date:
    """Today's date in India Standard Time (Asia/Kolkata)."""
    if IST is not None:
        return datetime.now(IST).date()
    return date.today()


def next_at_ist(hhmm: str) -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in IST.

    Returns None when the string is not a valid HH:MM. Used by the schedule so
    a report can be tied to an exact clock time (e.g. run at 09:15 IST) instead
    of only an interval - and it lands on that minute regardless of the host's
    timezone.
    """
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    if IST is None:
        now_local = _dt.datetime.now()
        cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now_local:
            cand += _dt.timedelta(days=1)
        return cand.timestamp()
    now_ist = now_utc.astimezone(IST)
    cand = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now_ist:
        cand += _dt.timedelta(days=1)
    return cand.timestamp()


def parse_iso_date(value) -> date | None:
    """Parse an ISO date string, returning None when unset/invalid."""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_nse_date(value: str) -> str:
    """Normalise date strings (e.g. '06-Aug-2026', '2026-08-06T00:00:00') to ISO '2026-08-06'.

    Unparsable values are returned unchanged so callers can display them raw.
    """
    val = str(value or "").strip()
    if not val or val == "-":
        return "-"
    if "T" in val:
        val = val.split("T")[0]
    elif " " in val:
        val = val.split()[0]

    for fmt in (
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y%m%d",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(val.title(), fmt).date().isoformat()
        except ValueError:
            pass
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except ValueError:
            pass
    return val


def fmt_date(value) -> str:
    """Pretty-print an ISO date as '07-Aug-2026' (raw string if unparsable)."""
    s = str(value or "")
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return s


def fmt_ts(ts) -> str:
    """Format a unix timestamp as '07-Aug 14:30' (or empty when absent)."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d-%b %H:%M")
    except (TypeError, ValueError, OSError):
        return ""
