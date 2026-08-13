"""Per-user alert preferences (settings.json).

Action-type filters, price-move threshold, watcher config and favourites live
here so they survive restarts and GitHub Actions re-runs.
"""
from __future__ import annotations

import logging
import time

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def load_settings() -> dict:
    """Return {chat_id(str): settings dict} for all users."""
    with _lock:
        data = read_json(config.SETTINGS_FILE, {})
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def get_user_settings(chat_id) -> dict:
    """Return the settings dict for a chat (empty dict when none stored)."""
    return load_settings().get(str(chat_id), {})


def save_user_settings(chat_id, settings: dict) -> None:
    """Merge/replace a chat's settings dict."""
    with _lock, _file_lock(config.SETTINGS_FILE):
        current = read_json(config.SETTINGS_FILE, {})
        current[str(chat_id)] = settings
        write_json(config.SETTINGS_FILE, current)
    log.info("settings.json: chat %s settings = %s", chat_id, settings)


RECENT_COMMANDS_LIMIT = 10


def record_recent_command(chat_id, text: str) -> None:
    """Remember the last commands a chat ran (most recent first, deduped).

    Powers the dynamic "recent commands" one-tap suggestions: the last
    RECENT_COMMANDS_LIMIT distinct command strings are stored per chat in
    settings.json so they survive restarts and are shown as tappable
    buttons for easy re-running.
    """
    raw = (text or "").strip()
    if not raw:
        return
    with _lock, _file_lock(config.SETTINGS_FILE):
        current = read_json(config.SETTINGS_FILE, {})
        settings = dict(current.get(str(chat_id)) or {})
        recent = [item for item in (settings.get("recent_commands") or []) if item != raw]
        recent.insert(0, raw)
        settings["recent_commands"] = recent[:RECENT_COMMANDS_LIMIT]
        current[str(chat_id)] = settings
        write_json(config.SETTINGS_FILE, current)


def get_recent_commands(chat_id, limit: int = RECENT_COMMANDS_LIMIT) -> list[str]:
    """The last commands a chat ran, most recent first (empty when none)."""
    settings = load_settings().get(str(chat_id)) or {}
    return (settings.get("recent_commands") or [])[:limit]


def ca_alerts_enabled(chat_id) -> bool:
    """Whether the chat wants corporate-action + ex-date reminder pushes.

    Controlled by /alertfilters off|on and /corpactions off|on. On-demand
    queries (/corpactions, /corpactionsformylist) always work - this only
    gates the automatic pushes.
    """
    settings = get_user_settings(chat_id)
    return bool(settings.get("ca_alerts", True))


def quiet_until_ts(chat_id) -> float | None:
    """Epoch seconds until which all alerts are paused (None = not paused)."""
    raw = get_user_settings(chat_id).get("quiet_until")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def is_quiet(chat_id, now: float | None = None) -> bool:
    """True when the chat has paused ALL outgoing pushes (/quiet).

    `quiet` flag = the pause is active; `quiet_until` None means "until told
    otherwise", a timestamp means it auto-resumes then. Command replies are
    never affected - only background pushes (CA alerts, reminders, price
    alerts, watcher, scheduled reports).
    """
    settings = get_user_settings(chat_id)
    if not settings.get("quiet"):
        return False
    until = quiet_until_ts(chat_id)
    if until is None:
        return True
    return until > (now if now is not None else time.time())
