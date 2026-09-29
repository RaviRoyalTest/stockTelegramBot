"""Record last-session market screens into data/snapshots.json.

One file holds the LAST session only: overnight gap-downs, session top
gainers/losers and the corporate-action list, stamped with the session
date. The web Sessions tab and the daily mail serve this file instead of
re-fetching hundreds of symbols on every view.

Recording is explicit (Telegram /snap, web "Record now", daily mail) or
skipped when the file already covers the latest session - never repeated
fetch churn.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, storage
from .core.dates import today_ist
from .opening_report.data import (
    fetch_universe_moves,
    latest_session_date,
    top_gainers,
    top_losers,
)
from .sources import get_bse_corporate_actions, get_gap_change, get_index_universe

log = logging.getLogger(__name__)

GAP_TOP_N = 20
MOVER_TOP_N = 10
ACTIONS_TOP_N = 100


def _round(value, digits: int = 2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def compact_gap_row(symbol: str, data: dict) -> dict:
    """One gap-down row with JSON-safe rounded numbers (pure)."""
    return {
        "symbol": symbol,
        "name": data.get("name") or symbol,
        "price": _round(data.get("price")),
        "prev_close": _round(data.get("prev_close")),
        "open": _round(data.get("open")),
        "gap_pct": _round(data.get("gap_pct")),
        "move_from_open_pct": _round(data.get("move_from_open_pct")),
    }


def compact_move_row(row: dict) -> dict:
    """One session mover row with JSON-safe rounded numbers (pure)."""
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "price": _round(row.get("price")),
        "prev_close": _round(row.get("prev_close")),
        "change": _round(row.get("change")),
        "change_pct": _round(row.get("change_pct")),
        "volume": row.get("volume"),
        "volume_change_pct": _round(row.get("volume_change_pct"), 1),
    }


def normalize_action(row: dict) -> dict:
    """One corporate-action row with the display columns (pure)."""
    return {
        "symbol": row.get("symbol") or "",
        "company": row.get("company") or "",
        "exchange": row.get("exchange") or "",
        "action": row.get("subject") or row.get("purpose") or "",
        "ex_date": row.get("ex_date") or "",
        "record_date": row.get("record_date") or "",
        "announcement_date": row.get("announcement_date") or "",
    }


def trim_actions(rows: list[dict], limit: int = ACTIONS_TOP_N) -> list[dict]:
    """Dated ex-dates first (ascending), undated last, capped (pure)."""
    def _sort_key(row: dict) -> tuple:
        date = (row.get("ex_date") or "").strip()
        return (date in ("", "-"), date)

    return [normalize_action(row) for row in sorted(rows, key=_sort_key)[:limit]]


def build_snapshot_doc(session: str, universe: str, gaps: list[dict],
                       gainers: list[dict], losers: list[dict],
                       actions: list[dict], recorded_by: str = "manual") -> dict:
    """Assemble the file doc (pure - no network, no disk)."""
    return {
        "session": session,
        "universe": universe,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recorded_by": recorded_by,
        "gap_downs": gaps,
        "top_gainers": gainers,
        "top_losers": losers,
        "corporate_actions": actions,
    }


def _fetch_gap(symbol: str) -> tuple[str, dict | None]:
    try:
        data = get_gap_change("NSE", symbol)
    except Exception:
        return symbol, None
    if not data or data.get("gap_pct") is None or data["gap_pct"] >= 0:
        return symbol, None
    return symbol, data


def record_session_snapshot(universe: str = "nifty500", force: bool = False,
                            recorded_by: str = "manual") -> dict:
    """Fetch the last session's screens and persist them. Never raises.

    Returns the doc (freshly recorded, or the existing one when it already
    covers the latest session and force is False). Returns {} only when
    every source failed.
    """
    try:
        session_date = latest_session_date("in") or today_ist()
    except Exception:
        session_date = today_ist()
    session = session_date.isoformat()
    try:
        existing = storage.load_snapshots() or {}
        if not force and existing.get("session") == session \
                and existing.get("universe") == universe and existing.get("gap_downs"):
            log.info("snapshots: %s already recorded - skipping refetch", session)
            return existing
    except Exception:
        pass

    try:
        symbols = get_index_universe(universe) or []
    except Exception as error:
        log.warning("snapshots: universe %s unavailable: %s", universe, error)
        return storage.load_snapshots() or {}

    moves: list[dict] = []
    try:
        moves = fetch_universe_moves("NSE", symbols)
    except Exception as error:
        log.warning("snapshots: session moves failed: %s", error)
    gainers = [compact_move_row(row) for row in top_gainers(moves, MOVER_TOP_N)]
    losers = [compact_move_row(row) for row in top_losers(moves, MOVER_TOP_N)]

    gaps: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(_fetch_gap, symbol): symbol for symbol in symbols}
            found = []
            for future in as_completed(futures):
                try:
                    symbol, data = future.result()
                except Exception:
                    continue
                if data:
                    found.append((symbol, data))
        found.sort(key=lambda item: item[1]["gap_pct"])
        gaps = [compact_gap_row(symbol, data) for symbol, data in found[:GAP_TOP_N]]
    except Exception as error:
        log.warning("snapshots: gap scan failed: %s", error)

    actions: list[dict] = []
    try:
        from .sources import get_nse_corporate_actions

        combined: list[dict] = []
        try:
            combined.extend(get_nse_corporate_actions(None) or [])
        except Exception as error:
            log.info("snapshots: NSE actions unavailable: %s", error)
        try:
            combined.extend(get_bse_corporate_actions() or [])
        except Exception as error:
            log.info("snapshots: BSE actions unavailable: %s", error)
        actions = trim_actions(combined)
    except Exception as error:
        log.warning("snapshots: corporate actions failed: %s", error)

    if not gaps and not gainers and not losers and not actions:
        log.warning("snapshots: every source failed for %s", session)
        return storage.load_snapshots() or {}

    doc = build_snapshot_doc(session, universe, gaps, gainers, losers, actions, recorded_by)
    try:
        storage.save_snapshots(doc)
    except Exception as error:
        log.warning("snapshots: save failed: %s", error)
    log.info(
        "snapshots: recorded %s (%s): %d gaps, %d/%d movers, %d actions",
        session, universe, len(gaps), len(gainers), len(losers), len(actions),
    )
    return doc
