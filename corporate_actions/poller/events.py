"""Corporate-action event identity and date-window helpers.

Pure functions used by the poller, the /next command and the dashboard to
decide which actions are new, upcoming, recently passed or pending.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..core.dates import today_ist


def event_key(action: dict) -> str:
    """Stable identity of an action for de-duplication (per chat)."""
    return "|".join(
        [
            action.get("exchange", ""),
            action.get("symbol", ""),
            action.get("subject", ""),
            action.get("ex_date", ""),
            action.get("record_date", ""),
        ]
    )


def parse_ex_date(value) -> date | None:
    """Parse an ISO ex-date, returning None when unset/invalid."""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


RECENT_PASSED_DAYS = 30  # how far back /next reports recently passed ex-dates


def within_reminder_window(
    ex_date, today: date | None = None, days: int | None = None
) -> bool:
    """True when ex_date is today or within the reminder window ahead."""
    parsed = parse_ex_date(ex_date)
    if parsed is None:
        return False
    from .. import config

    today = today or today_ist()
    days = config.REMINDER_DAYS if days is None else days
    if days <= 0:
        return False
    return today <= parsed <= today + timedelta(days=days)


def recently_passed(
    ex_date, today: date | None = None, days: int | None = None
) -> bool:
    """True when ex_date fell within the recent lookback window (ex-date
    passed in the last `days` days, today excluded).

    Used by /next to surface in-progress actions - a rights issue whose
    ex-date has just passed (subscription still open) or a dividend whose
    payment is still pending - that a pure upcoming-ex-date view misses.
    """
    parsed = parse_ex_date(ex_date)
    if parsed is None:
        return False
    today = today or today_ist()
    days = RECENT_PASSED_DAYS if days is None else days
    if days <= 0:
        return False
    return today - timedelta(days=days) <= parsed < today
