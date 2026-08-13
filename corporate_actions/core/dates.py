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


def next_at_in_tz_after(hhmm: str, tz_name: str | None, after_ts: float) -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in a
    tz, strictly AFTER `after_ts` (epoch seconds).

    Unlike next_at_in_tz this is relative to an arbitrary instant, so the
    schedule can chain clock times (e.g. after firing at 09:15 the next due
    is 15:30 the same day, then 09:15 the next day). Returns None when the
    string is not a valid HH:MM.
    """
    parsed = _valid_hhmm(hhmm)
    if parsed is None:
        return None
    hour, minute = parsed
    timezone = None
    try:
        from zoneinfo import ZoneInfo

        if tz_name:
            timezone = ZoneInfo(tz_name)
    except Exception:
        timezone = None
    if timezone is None:
        base = _datetime.datetime.fromtimestamp(after_ts)
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += _datetime.timedelta(days=1)
        return candidate.timestamp()
    base = _datetime.datetime.fromtimestamp(after_ts, timezone)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= base:
        candidate += _datetime.timedelta(days=1)
    return candidate.timestamp()


def next_at_in_tz(hhmm: str, tz_name: str | None = "Asia/Kolkata") -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in a tz.

    Returns None when the string is not a valid HH:MM. Used by the schedule so
    a report can be tied to an exact clock time (e.g. run at 09:15 IST) instead
    of only an interval - and it lands on that minute regardless of the host's
    timezone. `tz_name` picks the wall clock the HH:MM belongs to (IST for the
    Indian market, America/New_York for the US market).
    """
    return next_at_in_tz_after(
        hhmm, tz_name,
        _datetime.datetime.now(_datetime.timezone.utc).timestamp(),
    )


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


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def month_number(value) -> int | None:
    """Month number (1-12) for a month name/abbreviation, or None."""
    name = str(value or "").strip().lower()[:3]
    return _MONTHS.get(name)


def parse_date_token(value) -> date | None:
    """Parse a flexible user date token into a date, or None.

    Accepts ISO (2026-08-12), day-first (12-08-2026, 12/08/2026, 12.08.2026),
    8-digit (12082026) and month-name forms (12 Aug 2026, 12-Aug-2026,
    12aug2026, 12aug, aug12, 12 Aug). The year may be 2 or 4 digits; a
    missing year defaults to the current year. A bare number is never a
    date, and invalid dates (e.g. 31 Feb) return None.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    now = date.today()

    def _build(day: int, month: int, year: int | None) -> date | None:
        try:
            return date(year if year else now.year, month, day)
        except ValueError:
            return None

    # ISO / year-first: 2026-08-12, 2026-8-12
    match = re.fullmatch(r"(\d{4})[\-/.](\d{1,2})[\-/.](\d{1,2})", raw)
    if match:
        return _build(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    # Day-first numeric: 12-08-2026, 12/08/2026, 12.08.2026, 12-08-26
    match = re.fullmatch(r"(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{2,4})", raw)
    if match:
        year = int(match.group(3))
        if year < 100:
            year += 2000
        return _build(int(match.group(1)), int(match.group(2)), year)
    # 8-digit: 12082026 (tried as yyyymmdd then ddmmyyyy)
    if re.fullmatch(r"\d{8}", raw):
        first = _build(int(raw[6:8]), int(raw[4:6]), int(raw[0:4]))
        if first is not None:
            return first
        return _build(int(raw[0:2]), int(raw[2:4]), int(raw[4:8]))
    # A bare short number (12, 500) is a count, never a date.
    if raw.isdigit():
        return None
    # Day + month name: 12 Aug 2026, 12-Aug-26, 12aug2026, 12aug
    match = re.fullmatch(r"(\d{1,2})[\s\-_.]?([a-z]{3,9})(?:[\s\-_.]?(\d{2,4}))?", lowered)
    if match:
        month = month_number(match.group(2))
        year = int(match.group(3)) + 2000 if match.group(3) and len(match.group(3)) == 2 \
            else (int(match.group(3)) if match.group(3) else None)
        if month:
            return _build(int(match.group(1)), month, year)
    # Month name + day: aug 12, Aug-12-2026, aug12
    match = re.fullmatch(r"([a-z]{3,9})[\s\-_.]?(\d{1,2})(?:[\s\-_.]?(\d{2,4}))?", lowered)
    if match:
        month = month_number(match.group(1))
        year = int(match.group(3)) + 2000 if match.group(3) and len(match.group(3)) == 2 \
            else (int(match.group(3)) if match.group(3) else None)
        if month:
            return _build(int(match.group(2)), month, year)
    return None


def date_from_parts(tokens, index: int) -> tuple:
    """Build a date from command tokens starting at `index` -> (date, consumed).

    Handles a single token (12-08-2026, 12aug, aug12, 2026-08-12) and the
    split forms (12 Aug, Aug 12, 12 Aug 2026, Aug 12 2026). Returns
    (None, 0) when the tokens are not a date.
    """
    if index >= len(tokens):
        return None, 0
    parsed = parse_date_token(tokens[index])
    if parsed is not None:
        return parsed, 1

    def _build(day: int, month: int, year_token: str | None) -> tuple:
        year = None
        consumed = 2
        if year_token and str(year_token).isdigit() and len(str(year_token)) in (2, 4):
            year = int(year_token) + (2000 if len(str(year_token)) == 2 else 0)
            consumed = 3
        try:
            return date(year or date.today().year, month, day), consumed
        except ValueError:
            return None, 0

    # day + month name, optional year: "12 Aug", "12 Aug 2026"
    if (str(tokens[index]).isdigit() and 1 <= int(tokens[index]) <= 31
            and index + 1 < len(tokens)):
        month = month_number(tokens[index + 1])
        if month:
            year_token = tokens[index + 2] if index + 2 < len(tokens) else None
            return _build(int(tokens[index]), month, year_token)
    # month name + day, optional year: "Aug 12", "Aug 12 2026"
    month = month_number(tokens[index])
    if month and index + 1 < len(tokens) \
            and str(tokens[index + 1]).isdigit() and 1 <= int(tokens[index + 1]) <= 31:
        year_token = tokens[index + 2] if index + 2 < len(tokens) else None
        return _build(int(tokens[index + 1]), month, year_token)
    return None, 0


def sessions_back_estimate(target: date, today: date | None = None) -> int:
    """Rough trading-session count between today and a past date (+ buffer).

    Used to size history fetches for date-based screens - an exact count is
    not needed, only enough to cover the target date. Never returns 0.
    """
    today = today or date.today()
    days = (today - target).days
    if days <= 0:
        return 1
    return max(1, int(days * 5 / 7) + 4)


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
