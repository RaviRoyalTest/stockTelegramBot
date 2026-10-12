"""Persistence package.

One module per state file on top of a shared atomic-JSON base layer
(json_file.py). This facade re-exports the whole public API so existing
`from corporate_actions import storage` call sites keep working.
"""
import logging

from .schedule import (
    add_schedule_entry,
    clear_schedule,
    is_undeliverable_chat,
    load_schedule,
    load_schedule_for,
    pause_schedule,
    remove_schedule_entry,
    resume_schedule,
    save_schedule,
    schedule_next_due_ts,
    set_schedule_next_due,
)
from .maillog import load_mail_log, record_mail
from .migrate import migrate_legacy_state_files
from .openclose import (
    list_openclose_dates,
    load_openclose,
    merge_sections,
    migrate_openclose_file,
    save_openclose,
)
from .seen import load_seen, save_seen
from .snapshots import (
    archive_snapshot,
    list_snapshot_dates,
    load_snapshot_archive,
    load_snapshots,
    save_snapshots,
)
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
from .realtime import (
    append_snapshot,
    capture_command_output,
    cleanup_expired_snapshots,
    generate_snapshot_filename,
    save_json_snapshot,
    unique_snapshot_path,
)
from .stock_capture import capture_stock_batch, capture_stock_detail
from .watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
    replace_watchlist,
    save_watchlist,
    watchlist_key,
)

__all__ = [
    "load_mail_log",
    "record_mail",
    "migrate_legacy_state_files",
    "append_snapshot",
    "capture_command_output",
    "cleanup_expired_snapshots",
    "generate_snapshot_filename",
    "save_json_snapshot",
    "unique_snapshot_path",
    "capture_stock_detail",
    "capture_stock_batch",
    "list_openclose_dates",
    "load_openclose",
    "merge_sections",
    "migrate_openclose_file",
    "save_openclose",
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
    "archive_snapshot",
    "list_snapshot_dates",
    "load_snapshot_archive",
    "load_schedule",
    "load_schedule_for",
    "save_schedule",
    "add_schedule_entry",
    "remove_schedule_entry",
    "clear_schedule",
    "is_undeliverable_chat",
    "set_schedule_next_due",
    "schedule_next_due_ts",
    "pause_schedule",
    "resume_schedule",
]


# One-time moves of any leftover pre-reorg files into data/. No-op when
# there is nothing to move; never raises, so importing the storage
# package can never break the bot over a filesystem quirk. A failure is
# still logged - a silently skipped migration would leave state behind in
# the old location with no trace in the logs.
try:
    migrate_legacy_state_files()
except Exception as error:  # pragma: no cover - defensive, best effort only
    logging.getLogger(__name__).warning(
        "legacy state migration skipped (state may still sit in the old path): %s", error
    )
try:
    migrate_openclose_file()
except Exception as error:  # pragma: no cover - defensive, best effort only
    logging.getLogger(__name__).warning(
        "legacy open/close migration skipped: %s", error
    )
