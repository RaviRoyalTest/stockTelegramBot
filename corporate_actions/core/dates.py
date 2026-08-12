"""Date/time helpers used by the poller, bot and dashboard.

Everything here is pure - no network, no storage, no Telegram. The host often
runs on UTC where the calendar flips at 18:30 IST, so all "today" logic must
follow the market's calendar (Asia/Kolkata), not the host's local date.
"""
from __future__ import annotations

import datetime as _datetime
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


def _valid_hhmm(hhmm) -> tuple | None:
    """(hour, minute) for a valid 'HH:MM' string, or None."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def next_at_in_tz(hhmm: str, tz_name: str | None = "Asia/Kolkata") -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in a tz.

    Returns None when the string is not a valid HH:MM. Used by the schedule so
    a report can be tied to an exact clock time (e.g. run at 09:15 IST) instead
    of only an interval - and it lands on that minute regardless of the host's
    timezone. `tz_name` picks the wall clock the HH:MM belongs to (IST for the
    Indian market, America/New_York for the US market).
    """
    parsed = _valid_hhmm(hhmm)
    if parsed is None:
        return None
    hour, minute = parsed
    now_utc = _datetime.datetime.now(_datetime.timezone.utc)
    timezone = None
    try:
        from zoneinfo import ZoneInfo

        if tz_name:
            timezone = ZoneInfo(tz_name)
    except Exception:
        timezone = None
    if timezone is None:
        now_local = _datetime.datetime.now()
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += _datetime.timedelta(days=1)
        return candidate.timestamp()
    now_tz = now_utc.astimezone(timezone)
    candidate = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_tz:
        candidate += _datetime.timedelta(days=1)
    return candidate.timestamp()


def next_at_ist(hhmm: str) -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in IST.

    Returns None when the string is not a valid HH:MM. Used by the schedule so
    a report can be tied to an exact clock time (e.g. run at 09:15 IST) instead
    of only an interval - and it lands on that minute regardless of the host's
    timezone.
    """
    return next_at_in_tz(hhmm, "Asia/Kolkata")


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
    value = str(value or "").strip()
    if not value or value == "-":
        return "-"
    if "T" in value:
        value = value.split("T")[0]
    elif " " in value:
        value = value.split()[0]

    for date_format in (
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y%m%d",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(value.title(), date_format).date().isoformat()
        except ValueError:
            pass
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return value


def format_date(value) -> str:
    """Pretty-print an ISO date as '07-Aug-2026' (raw string if unparsable)."""
    date_string = str(value or "")
    try:
        return datetime.strptime(date_string.strip(), "%Y-%m-%d").strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return date_string


def format_timestamp(timestamp) -> str:
    """Format a unix timestamp as '07-Aug 14:30' (or empty when absent)."""
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%d-%b %H:%M")
    except (TypeError, ValueError, OSError):
        return ""
