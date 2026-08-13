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


def action_is_completed(action: dict, today: date | None = None) -> bool:
    """True when a corporate action is fully settled (nothing pending).

    The /corpactionsformylist "recently passed" group is meant to surface
    actions that are still in progress - a rights subscription window that
    is open, a dividend whose payment is still due. Once the underlying
    event has fully settled (offer closed, payment window passed, shares
    re-denominated) the action is *completed* and is hidden from the report.
    """
    today = today or today_ist()
    from ..sources.types import action_type

    type_name = action_type(action.get("subject"))
    ex_date = parse_ex_date(action.get("ex_date"))
    if ex_date is None or ex_date >= today:
        return False  # pending / upcoming - never "completed"

    if type_name == "rights":
        end_date = parse_ex_date(action.get("rights_end"))
        if end_date is not None:
            return today > end_date  # offer window closed
        return False  # window unknown - keep surfacing it
    if type_name == "dividend":
        record_date = parse_ex_date(action.get("record_date"))
        if record_date is not None:
            return today > record_date + timedelta(days=30)  # payment window passed
        return False  # payment still pending
    if type_name == "bonus":
        return False  # credit is ~2 weeks out; keep it visible
    if type_name == "buyback":
        return False  # offer still open
    if type_name == "split":
        return True  # re-denominated at ex-date - settled
    return False  # generic: keep it visible rather than silently drop it
