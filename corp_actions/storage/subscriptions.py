"""Per-user subscription lists (subscriptions.json).

The owner's list is the app watchlist (watchlist.json). Other Telegram users
each get their own subscription list and receive alerts in their own chat.
"""
from __future__ import annotations

import logging

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json
from .watchlist import watchlist_key

log = logging.getLogger(__name__)


def load_subscriptions() -> dict:
    """Return {chat_id(str): [item, ...]} for non-owner users."""
    with _lock:
        data = read_json(config.SUBSCRIPTIONS_FILE, {})
    cleaned = {}
    for k, v in data.items():
        if isinstance(v, list):
            cleaned[str(k)] = [i for i in v if isinstance(i, dict)]
    return cleaned


def add_subscription(chat_id, item: dict) -> list:
    """Append an item to one chat's subscription list (de-duplicated). Returns the new list."""
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        current = subs.get(key, [])
        before = len(current)
        seen = {watchlist_key(i) for i in current}
        added = item.get("symbol") and watchlist_key(item) not in seen
        if added:
            current.append(item)
        subs[key] = current
        write_json(config.SUBSCRIPTIONS_FILE, subs)
    log.info(
        "subscriptions.json: chat %s %d -> %d item(s) | %s %s:%s",
        key, before, len(current),
        "added" if added else "skipped (already present)",
        item.get("exchange", "").upper(),
        item.get("symbol", "").upper(),
    )
    return current


def remove_subscription(chat_id, symbol: str, exchange: str) -> list:
    """Remove an item from one chat's subscription list. Returns the new list."""
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        before = len(subs.get(key, []))
        current = [
            i for i in subs.get(key, [])
            if not (i.get("symbol", "").upper() == symbol.upper()
                    and i.get("exchange", "").upper() == exchange.upper())
        ]
        subs[key] = current
        write_json(config.SUBSCRIPTIONS_FILE, subs)
    log.info(
        "subscriptions.json: chat %s %d -> %d item(s) | removed %s:%s",
        key, before, len(current), exchange.upper(), symbol.upper(),
    )
    return current
