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
    for key, value in data.items():
        if isinstance(value, list):
            cleaned[str(key)] = [item for item in value if isinstance(item, dict)]
    return cleaned


def add_subscription(chat_id, item: dict) -> list:
    """Append an item to one chat's subscription list (de-duplicated)."""
    return bulk_add_subscriptions(chat_id, [item])["list"]


def bulk_add_subscriptions(chat_id, items: list) -> dict:
    """Atomically add a batch, de-duplicating on (exchange, symbol)."""
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        current = subs.get(key)
        current = [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []
        seen = {watchlist_key(item) for item in current}
        added, duplicates = 0, 0
        for item in items:
            if not isinstance(item, dict) or not watchlist_key(item)[1]:
                continue
            item_key = watchlist_key(item)
            if item_key in seen:
                duplicates += 1
                continue
            current.append(item)
            seen.add(item_key)
            added += 1
        subs[key] = current
        write_json(config.SUBSCRIPTIONS_FILE, subs)
    return {"list": current, "added": added, "skipped_duplicates": duplicates}


def replace_subscriptions(chat_id, items: list) -> dict:
    """Replace one chat's subscription list with exactly ``items`` (deduped).

    Returns {"list": new_list, "added": n, "skipped_duplicates": n}.
    """
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        fresh: list = []
        seen: set = set()
        dups = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            k = watchlist_key(item)
            if k in seen:
                dups += 1
                continue
            seen.add(k)
            fresh.append(item)
        before = len(subs.get(key, []))
        subs[key] = fresh
        write_json(config.SUBSCRIPTIONS_FILE, subs)
    log.info(
        "subscriptions.json: chat %s %d -> %d item(s) (exact set, %d duplicate(s) skipped)",
        key, before, len(fresh), dups,
    )
    return {"list": fresh, "added": len(fresh), "skipped_duplicates": dups}


def remove_subscription(chat_id, symbol: str, exchange: str) -> list:
    """Remove an item from one chat's subscription list. Returns the new list."""
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        before = len(subs.get(key, []))
        current = [
            item for item in subs.get(key, [])
            if not (item.get("symbol", "").upper() == symbol.upper()
                    and item.get("exchange", "").upper() == exchange.upper())
        ]
        subs[key] = current
        write_json(config.SUBSCRIPTIONS_FILE, subs)
    log.info(
        "subscriptions.json: chat %s %d -> %d item(s) | removed %s:%s",
        key, before, len(current), exchange.upper(), symbol.upper(),
    )
    return current
