"""Recorded market snapshots (data/snapshots.json).

Holds the LAST session's screens so the web Sessions tab (and the daily
mail) serve them from disk instead of re-fetching hundreds of symbols on
every page load: overnight gap-downs, session top gainers/losers and the
corporate-action list, all stamped with the session date they belong to.
"""
from __future__ import annotations

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json


def load_snapshots() -> dict:
    """Return the recorded snapshot doc ({} when never recorded)."""
    with _lock, _file_lock(config.SNAPSHOT_FILE):
        data = read_json(config.SNAPSHOT_FILE, {})
    return data if isinstance(data, dict) else {}


def save_snapshots(doc: dict) -> None:
    """Persist one snapshot doc, atomically."""
    with _lock, _file_lock(config.SNAPSHOT_FILE):
        write_json(config.SNAPSHOT_FILE, doc or {})
