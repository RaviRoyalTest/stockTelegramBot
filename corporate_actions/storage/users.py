"""User-scoped watchlist operations.

Every chat has exactly one list: the owner's lives in watchlist.json, every
other user's lives in subscriptions.json keyed by chat id. This module is the
single entry point so callers never branch on ownership themselves.
"""
from __future__ import annotations

from .. import config
from .subscriptions import (
    add_subscription,
    bulk_add_subscriptions,
    load_subscriptions,
    remove_subscription,
    replace_subscriptions,
)
from .watchlist import (
    add_to_watchlist,
    bulk_add_watchlist,
    load_watchlist,
    remove_from_watchlist,
    replace_watchlist,
)


def is_owner(chat_id) -> bool:
    return str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def list_location(chat_id) -> str:
    """Human label of where this chat's list is stored.

    Used by every report footer and by the scheduled-report attribution so
    the user always knows which list the results relate to.
    """
    if is_owner(chat_id):
        return "data/watchlist.json (owner's list)"
    return f"data/subscriptions.json (chat {chat_id})"


def get_user_list(chat_id) -> list:
    if is_owner(chat_id):
        return load_watchlist()
    subs = load_subscriptions()
    return subs.get(str(chat_id), [])


def add_to_user_list(chat_id, item: dict) -> list:
    if is_owner(chat_id):
        return add_to_watchlist([item])
    return add_subscription(chat_id, item)


def bulk_add_to_user_list(chat_id, items: list) -> dict:
    """Add many items at once, skipping ones already present. Returns a summary."""
    if is_owner(chat_id):
        return bulk_add_watchlist(items)
    return bulk_add_subscriptions(chat_id, items)


def set_user_list_exact(chat_id, items: list) -> dict:
    """Replace the chat's list with exactly ``items`` (dedup applied). Returns a summary."""
    if is_owner(chat_id):
        return replace_watchlist(items)
    return replace_subscriptions(chat_id, items)


def remove_from_user_list(chat_id, symbol: str, exchange: str) -> list:
    if is_owner(chat_id):
        return remove_from_watchlist(symbol, exchange)
    return remove_subscription(chat_id, symbol, exchange)
