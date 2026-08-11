"""De-duplication cache (seen_actions.json).

Stores event keys already notified so nothing is re-sent across restarts or
GitHub Actions re-runs.
"""
from __future__ import annotations

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json


def load_seen() -> set:
    """Return set of event keys already notified (survives restarts)."""
    with _lock, _file_lock(config.SEEN_FILE):
        data = read_json(config.SEEN_FILE, [])
    return set(data) if isinstance(data, list) else set()


def save_seen(keys: set) -> None:
    with _lock, _file_lock(config.SEEN_FILE):
        write_json(config.SEEN_FILE, sorted(keys))
