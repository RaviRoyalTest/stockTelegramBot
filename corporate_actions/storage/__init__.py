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
    pause_schedule,
    remove_schedule_entry,
    resume_schedule,
    save_schedule,
    schedule_next_due_ts,
    set_schedule_next_due,
)
from .migrate import migrate_legacy_state_files
from .seen import load_seen, save_seen
from .snapshots import load_snapshots, save_snapshots
from .settings import (
    ca_alerts_enabled,
    get_recent_commands,
    get_user_settings,
    is_quiet,
    load_settings,
    quiet_until_ts,
    record_recent_command,
    save_user_settings,
)
from .subscriptions import (
    add_subscription,
    load_subscriptions,
    remove_subscription,
    replace_subscriptions,
)
from .users import (
    add_to_user_list,
    bulk_add_to_user_list,
    get_user_list,
    is_owner,
    list_location,
    remove_from_user_list,
    set_user_list_exact,
)
from .watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
    replace_watchlist,
    save_watchlist,
    watchlist_key,
)

__all__ = [
    "migrate_legacy_state_files",
    "load_watchlist",
    "save_watchlist",
    "watchlist_key",
    "add_to_watchlist",
    "remove_from_watchlist",
    "replace_watchlist",
    "load_subscriptions",
    "add_subscription",
    "remove_subscription",
    "replace_subscriptions",
    "is_owner",
    "list_location",
    "get_user_list",
    "add_to_user_list",
    "bulk_add_to_user_list",
    "set_user_list_exact",
    "remove_from_user_list",
    "load_settings",
    "get_user_settings",
    "save_user_settings",
    "ca_alerts_enabled",
    "is_quiet",
    "quiet_until_ts",
    "record_recent_command",
    "get_recent_commands",
    "load_seen",
    "save_seen",
    "load_snapshots",
    "save_snapshots",
    "load_schedule",
    "load_schedule_for",
    "save_schedule",
    "add_schedule_entry",
    "remove_schedule_entry",
    "clear_schedule",
    "set_schedule_next_due",
    "schedule_next_due_ts",
    "pause_schedule",
    "resume_schedule",
]


# One-time move of any leftover repo-root state files (pre-reorg copies)
# into data/. No-op when there is nothing to move; never raises, so importing
# the storage package can never break the bot over a filesystem quirk.
try:
    migrate_legacy_state_files()
except Exception:  # pragma: no cover - defensive, best effort only
    pass
