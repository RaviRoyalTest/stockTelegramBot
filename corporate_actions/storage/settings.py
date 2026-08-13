"""Per-user alert preferences (settings.json).

Action-type filters, price-move threshold, watcher config and favourites live
here so they survive restarts and GitHub Actions re-runs.
"""
from __future__ import annotations

import logging

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
