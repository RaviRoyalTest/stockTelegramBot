"""Watchlist persistence (the owner's list in watchlist.json)."""
from __future__ import annotations

import logging

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def load_watchlist() -> list[dict]:
    """Return list of {'symbol', 'company', 'exchange'}."""
    with _lock:
        data = read_json(config.WATCHLIST_FILE, [])
    return [item for item in data if isinstance(item, dict)]


def save_watchlist(watchlist: list[dict]) -> None:
    with _lock:
        write_json(config.WATCHLIST_FILE, watchlist)


def watchlist_key(item: dict) -> tuple:
    return (item.get("exchange", "").upper(), item.get("symbol", "").upper())


def add_to_watchlist(items: list[dict]) -> list[dict]:
    """Add entries, de-duplicating on (exchange, symbol). Returns new list."""
    with _lock, _file_lock(config.WATCHLIST_FILE):
        current = read_json(config.WATCHLIST_FILE, [])
        before = len(current)
        seen = {watchlist_key(item) for item in current}
        added_keys, skipped = [], []
        for item in items:
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
    return current


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
