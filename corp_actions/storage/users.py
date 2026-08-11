"""User-scoped watchlist operations.

Every chat has exactly one list: the owner's lives in watchlist.json, every
other user's lives in subscriptions.json keyed by chat id. This module is the
single entry point so callers never branch on ownership themselves.
"""
from __future__ import annotations

from .. import config
from .subscriptions import add_subscription, load_subscriptions, remove_subscription
from .watchlist import add_to_watchlist, load_watchlist, remove_from_watchlist


def is_owner(chat_id) -> bool:
    return str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def get_user_list(chat_id) -> list:
    if is_owner(chat_id):
        return load_watchlist()
    subs = load_subscriptions()
    return subs.get(str(chat_id), [])


def add_to_user_list(chat_id, item: dict) -> list:
    if is_owner(chat_id):
        return add_to_watchlist([item])
    return add_subscription(chat_id, item)


def remove_from_user_list(chat_id, symbol: str, exchange: str) -> list:
    if is_owner(chat_id):
        return remove_from_watchlist(symbol, exchange)
    return remove_subscription(chat_id, symbol, exchange)
