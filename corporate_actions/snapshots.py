"""Record last-session market screens into data/snapshots.json.

One file holds the LAST session only, mirroring the openreport layout:
India = NIFTY 100 + NIFTY 500 ex-100 + Microcap 250 blocks of 20
(10 gainers + 10 losers each = 60 rows, exactly like /openreport);
US = Mega-cap + Large-cap blocks. Plus the overnight gap-down scan and
the corporate-action list, all stamped with the session date.

The web Sessions tab and the daily mail serve this file instead of
re-fetching hundreds of symbols on every view. Recording is explicit
(Telegram /snap, web "Record now", daily mail) or skipped when the file
already covers the latest session - never repeated fetch churn.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, storage
from .core.dates import today_ist
from .opening_report.data import (
    fetch_universe_moves,
    get_microcap250,
    get_nifty100,
    get_nifty500_ex_100,
    get_us_market_caps,
    get_us_universe,
    latest_session_date,
    split_us_by_cap,
    top_gainers,
    top_losers,
)
from .sources import get_bse_corporate_actions, get_gap_change, get_index_universe

log = logging.getLogger(__name__)

GAP_TOP_N = 20
MOVER_TOP_N = 10
ACTIONS_TOP_N = 100

# Openreport-style blocks per market: (key, title, symbol-loader).
IN_BLOCKS = (
    ("in100", "NIFTY 100", get_nifty100),
    ("in500x", "NIFTY 500 EX-NIFTY 100", get_nifty500_ex_100),
    ("inmicro", "NIFTY MICROCAP 250", get_microcap250),
)


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
                       actions: list[dict], recorded_by: str = "manual",
                       market: str = "in", universes: list[dict] | None = None) -> dict:
    """Assemble the file doc (pure - no network, no disk)."""
    return {
        "session": session,
        "market": market,
        "universe": universe,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recorded_by": recorded_by,
        "gap_downs": gaps,
        "top_gainers": gainers,
        "top_losers": losers,
        "universes": universes or [],
        "corporate_actions": actions,
    }


def _fetch_gap(exchange: str, symbol: str) -> tuple[str, dict | None]:
    try:
        data = get_gap_change(exchange, symbol)
    except Exception:
        return symbol, None
    if not data or data.get("gap_pct") is None or data["gap_pct"] >= 0:
        return symbol, None
    return symbol, data


def _universe_block(key: str, title: str, exchange: str,
                    symbols: list[str]) -> tuple[dict, list[dict]]:
    """One openreport-style block + its raw rows (for the flat tops)."""
    try:
        rows = fetch_universe_moves(exchange, symbols or [])
    except Exception as error:
        log.warning("snapshots: block %s failed: %s", key, error)
        rows = []
    block = {
        "key": key,
        "title": title,
        "gainers": [compact_move_row(row) for row in top_gainers(rows, MOVER_TOP_N)],
        "losers": [compact_move_row(row) for row in top_losers(rows, MOVER_TOP_N)],
    }
    return block, rows


def _market_key(market: str, universe: str | None) -> tuple[str, str]:
    """Normalize (market, universe-label) from either argument (pure)."""
    text = f"{market or ''} {universe or ''}".lower()
    if any(token in text for token in ("us", "nasdaq", "sp500", "s&p")):
        return "us", "us"
    return "in", (universe or "nifty500").lower() or "nifty500"


def record_session_snapshot(universe: str = "nifty500", force: bool = False,
                            recorded_by: str = "manual",
                            market: str | None = None) -> dict:
    """Fetch the last session's screens and persist them. Never raises.

    market "in" records the India openreport layout (NIFTY 100 + 500 ex-100
    + Microcap 250 blocks of 20 = 60 rows); "us" records Mega + Large-cap
    blocks. Flat top gainers/losers cover the whole market for the daily
    mail. Returns the doc (freshly recorded, or the existing one when it
    already covers the latest session and force is False). Returns {}
    only when every source failed.
    """
    market, universe = _market_key(market or "", universe)
    exchange = "US" if market == "us" else "NSE"
    try:
        session_date = latest_session_date(market) or today_ist()
    except Exception:
        session_date = today_ist()
    session = session_date.isoformat()
    try:
        existing = storage.load_snapshots() or {}
        if not force and existing.get("session") == session \
                and existing.get("market", "in") == market and existing.get("gap_downs"):
            log.info("snapshots: %s/%s already recorded - skipping refetch", market, session)
            return existing
    except Exception:
        pass

    blocks: list[dict] = []
    all_rows: list[dict] = []
    gap_symbols: list[str] = []
    if market == "us":
        try:
            us_symbols = get_us_universe() or []
        except Exception as error:
            log.warning("snapshots: US universe unavailable: %s", error)
            us_symbols = []
        us_rows: list[dict] = []
        try:
            us_rows = fetch_universe_moves("US", us_symbols)
        except Exception as error:
            log.warning("snapshots: US moves failed: %s", error)
        try:
            caps = get_us_market_caps([row["symbol"] for row in us_rows])
        except Exception:
            caps = {}
        try:
            mega_rows, large_rows, _unclassified = split_us_by_cap(us_rows, caps)
        except Exception:
            mega_rows, large_rows = [], []
        for key, title, bucket in (("usmega", "MEGA CAP ($200B+)", mega_rows),
                                  ("uslarge", "LARGE CAP ($10B-$200B)", large_rows)):
            blocks.append({
                "key": key,
                "title": title,
                "gainers": [compact_move_row(row) for row in top_gainers(bucket, MOVER_TOP_N)],
                "losers": [compact_move_row(row) for row in top_losers(bucket, MOVER_TOP_N)],
            })
        all_rows = us_rows
        gap_symbols = us_symbols
    else:
        for key, title, loader in IN_BLOCKS:
            try:
                symbols = loader() or []
            except Exception as error:
                log.warning("snapshots: block %s universe failed: %s", key, error)
                symbols = []
            block, rows = _universe_block(key, title, "NSE", symbols)
            blocks.append(block)
            all_rows.extend(rows)
        try:
            gap_symbols = get_index_universe("nifty500") or []
        except Exception as error:
            log.warning("snapshots: gap universe unavailable: %s", error)
            gap_symbols = []

    gainers = [compact_move_row(row) for row in top_gainers(all_rows, MOVER_TOP_N)]
    losers = [compact_move_row(row) for row in top_losers(all_rows, MOVER_TOP_N)]

    gaps: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(_fetch_gap, exchange, symbol): symbol
                       for symbol in gap_symbols}
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
    if market == "in":
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

    if not gaps and not gainers and not losers and not actions \
            and not any(block.get("gainers") or block.get("losers") for block in blocks):
        log.warning("snapshots: every source failed for %s/%s", market, session)
        return storage.load_snapshots() or {}

    doc = build_snapshot_doc(session, universe, gaps, gainers, losers, actions,
                             recorded_by, market, blocks)
    try:
        storage.save_snapshots(doc)
    except Exception as error:
        log.warning("snapshots: save failed: %s", error)
    log.info(
        "snapshots: recorded %s/%s: %d gaps, %d/%d movers, %d blocks, %d actions",
        market, session, len(gaps), len(gainers), len(losers),
        len(blocks), len(actions),
    )
    return doc
