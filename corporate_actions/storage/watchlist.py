"""Watchlist persistence (the owner's list in watchlist.json)."""
from __future__ import annotations

import logging

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def _valid_watchlist_item(item) -> bool:
    return (isinstance(item, dict)
            and isinstance(item.get("symbol"), str)
            and bool(item["symbol"].strip())
            and all(isinstance(item[field], str)
                    for field in ("exchange", "company") if field in item))


def _validate_watchlist(items) -> None:
    if not isinstance(items, list):
        raise ValueError("watchlist must be a list")
    for index, item in enumerate(items):
        if not _valid_watchlist_item(item):
            raise ValueError(f"watchlist item {index} requires a nonempty string symbol "
                             "and string exchange/company when present")


def load_watchlist() -> list[dict]:
    """Return validated {'symbol', optional 'company'/'exchange'} entries."""
    return read_json(config.WATCHLIST_FILE, [], validator=_validate_watchlist)


def save_watchlist(watchlist: list[dict]) -> None:
    with _lock, _file_lock(config.WATCHLIST_FILE):
        write_json(config.WATCHLIST_FILE, watchlist)


def watchlist_key(item: dict) -> tuple:
    return (str(item.get("exchange") or "").strip().upper(),
            str(item.get("symbol") or "").strip().upper())


def add_to_watchlist(items: list[dict]) -> list[dict]:
    """Add entries, de-duplicating on (exchange, symbol). Returns new list."""
    return bulk_add_watchlist(items)["list"]


def bulk_add_watchlist(items: list[dict]) -> dict:
    """Atomically add entries and return list/added/skipped_duplicates."""
    with _lock, _file_lock(config.WATCHLIST_FILE):
        current = [item for item in read_json(config.WATCHLIST_FILE, [])
                   if isinstance(item, dict)]
        before = len(current)
        seen = {watchlist_key(item) for item in current}
        added_keys, skipped = [], []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = watchlist_key(item)
            if key not in seen and item.get("symbol"):
                current.append(item)
                seen.add(key)
                added_keys.append(key)
            elif key in seen:
                skipped.append(key)
        write_json(config.WATCHLIST_FILE, current)
    log.info(
        "watchlist.json: %d -> %d item(s) | added: %s | skipped (already present): %s",
        before,
        len(current),
        ", ".join(f"{key[0]}:{key[1]}" for key in added_keys) or "none",
        ", ".join(f"{key[0]}:{key[1]}" for key in skipped) or "none",
    )
    return {"list": current, "added": len(added_keys),
            "skipped_duplicates": len(skipped)}


def replace_watchlist(items: list[dict]) -> dict:
    """Replace the watchlist with exactly ``items`` (dedup on exchange+symbol).

    Returns {"list": new_list, "added": n, "skipped_duplicates": n} where
    "added" counts the entries in the new list and duplicates were dropped.
    """
    with _lock, _file_lock(config.WATCHLIST_FILE):
        current = read_json(config.WATCHLIST_FILE, [])
        fresh: list[dict] = []
        seen: set = set()
        dups = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            key = watchlist_key(item)
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            fresh.append(item)
        write_json(config.WATCHLIST_FILE, fresh)
    log.info(
        "watchlist.json: %d -> %d item(s) (exact set) | %d duplicate(s) skipped",
        len(current), len(fresh), dups,
    )
    return {"list": fresh, "added": len(fresh), "skipped_duplicates": dups}


def remove_from_watchlist(symbol: str, exchange: str) -> list[dict]:
    with _lock, _file_lock(config.WATCHLIST_FILE):
        current = read_json(config.WATCHLIST_FILE, [])
        before = len(current)
        # Items stored without an exchange (older dashboard adds) match any
        # requested exchange so they can still be removed.
        kept = [
            item
            for item in current
            if not (item.get("symbol", "").upper() == symbol.upper()
                    and (not item.get("exchange")
                         or item.get("exchange", "").upper() == exchange.upper()))
        ]
        write_json(config.WATCHLIST_FILE, kept)
    log.info(
        "watchlist.json: %d -> %d item(s) | removed %s:%s",
        before, len(kept), exchange.upper(), symbol.upper(),
    )
    return kept
