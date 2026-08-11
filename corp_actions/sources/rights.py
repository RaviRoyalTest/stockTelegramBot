"""Curated rights-offer subscription windows.

The NSE/BSE corporate-action feeds announce a rights issue with ex-date /
record-date but NEVER the subscription (offer) window - those dates come only
from the offer document (RHP/LOF) filed later. So we keep a small map of
currently-active rights issues here, symbol -> (offer open, offer close) ISO
dates, and attach them to the records below. Add new live rights issues here
as they are announced; stale entries are harmless (the block only shows the
window while both dates are in the future or one is recent).
"""
from datetime import date

from ..core.dates import today_ist
from .types import action_type

RIGHTS_OFFER_WINDOWS: dict[str, tuple[str, str]] = {
    "GENESYS": ("2026-08-14", "2026-08-21"),
}


def attach_rights_windows(records: list[dict]) -> list[dict]:
    """Attach rights offer-window dates to rights records when known.

    Records may already carry structured fields (e.g. if a feed adds
    rightsStartDate/rightsEndDate later) - those win. Otherwise the curated
    RIGHTS_OFFER_WINDOWS map fills the gap.
    """

    def _has(value) -> bool:
        return bool(value) and str(value).strip() not in ("", "-")

    today = today_ist()
    for r in records:
        if action_type(r.get("subject")) != "rights":
            continue
        if _has(r.get("rights_start")) and _has(r.get("rights_end")):
            continue
        win = RIGHTS_OFFER_WINDOWS.get((r.get("symbol") or "").upper())
        if not win:
            continue
        try:
            start = date.fromisoformat(win[0])
            end = date.fromisoformat(win[1])
        except (TypeError, ValueError):
            continue
        # Only attach to the CURRENT issue. A rights record's ex/record date
        # sits days-weeks BEFORE its offer window, so an ancient historical
        # rights record of the same symbol (ex-date far from the window) must
        # never receive this window - and a window long past is stale too.
        ex = None
        if r.get("ex_date") and str(r["ex_date"]).strip() not in ("", "-"):
            try:
                ex = date.fromisoformat(str(r["ex_date"]))
            except (TypeError, ValueError):
                ex = None
        if ex is not None and abs((ex - start).days) > 45:
            continue
        if abs((end - today).days) > 90:
            continue
        r["rights_start"], r["rights_end"] = win
    return records
