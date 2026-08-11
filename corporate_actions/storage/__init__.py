"""Persistence package.

One module per state file on top of a shared atomic-JSON base layer
(json_file.py). This facade re-exports the whole public API so existing
`from corporate_actions import storage` call sites keep working.
"""
from .schedule import (
    add_schedule_entry,
    clear_schedule,
    load_schedule,
    load_schedule_for,
    remove_schedule_entry,
    save_schedule,
    schedule_next_due_ts,
    set_schedule_next_due,
)
from .seen import load_seen, save_seen
from .settings import get_user_settings, load_settings, save_user_settings
from .subscriptions import add_subscription, load_subscriptions, remove_subscription
from .users import (
    add_to_user_list,
    get_user_list,
    is_owner,
    remove_from_user_list,
)
from .watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
    save_watchlist,
    watchlist_key,
)

__all__ = [
    "load_watchlist",
    "save_watchlist",
    "watchlist_key",
    "add_to_watchlist",
    "remove_from_watchlist",
    "load_subscriptions",
    "add_subscription",
    "remove_subscription",
    "is_owner",
    "get_user_list",
    "add_to_user_list",
    "remove_from_user_list",
    "load_settings",
    "get_user_settings",
    "save_user_settings",
    "load_seen",
    "save_seen",
    "load_schedule",
    "load_schedule_for",
    "save_schedule",
    "add_schedule_entry",
    "remove_schedule_entry",
    "clear_schedule",
    "set_schedule_next_due",
    "schedule_next_due_ts",
]
