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
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


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
