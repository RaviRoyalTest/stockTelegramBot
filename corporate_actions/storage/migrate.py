"""One-time migration of legacy repo-root state files into data/.

Before the state-file reorganization, user updates landed directly in
watchlist.json / subscriptions.json / settings.json / seen_actions.json /
schedule.json at the repo root. git mv handles tracked files on pull, but
any copy left behind at the old location (uncommitted edits, a stale
ephemeral disk, a host running mixed revisions) must never silently fork
state: if the data/ file is missing and the legacy root file exists, move
it into place. Never overwrites - the data/ copy always wins.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .json_file import _file_lock

log = logging.getLogger(__name__)

# (legacy repo-root name, new config attribute) pairs.
_LEGACY_MAP = (
    ("watchlist.json", "WATCHLIST_FILE"),
    ("subscriptions.json", "SUBSCRIPTIONS_FILE"),
    ("settings.json", "SETTINGS_FILE"),
    ("seen_actions.json", "SEEN_FILE"),
    ("schedule.json", "SCHEDULE_FILE"),
)


def migrate_legacy_state_files(base_dir: Path | None = None) -> list[str]:
    """Move leftover repo-root state files into data/. Returns moved names.

    Pure function of (base_dir, config paths): pass an explicit base_dir in
    tests to avoid touching the real repo. Idempotent and safe to call on
    every import - it only moves when the new location is missing.
    """
    from .. import config

    root = Path(base_dir) if base_dir is not None else config.BASE_DIR
    moved: list[str] = []
    for legacy_name, config_attr in _LEGACY_MAP:
        try:
            new_path = Path(getattr(config, config_attr))
        except AttributeError:
            continue
        if not new_path.is_absolute():
            new_path = root / new_path
        legacy_path = root / legacy_name
        try:
            # Check and move under the same shared lock as normal writers.
            with _file_lock(new_path):
                if new_path.exists() or not legacy_path.is_file():
                    continue
                new_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(legacy_path, new_path)
                moved.append(legacy_name)
                log.info("migrated legacy state file %s -> %s", legacy_name, new_path)
        except OSError as error:
            log.warning("could not migrate %s: %s", legacy_name, error)
    return moved


def normalize_schedule_timestamps(path: Path | None = None) -> int:
    """Stamp legacy naive next_due/paused_until values as explicit UTC.

    Old writers stored ``datetime.fromtimestamp(ts).isoformat()`` - the HOST's
    wall clock, which is UTC on every deployed host. The instant was therefore
    correct but unlabelled, so a reader on an IST machine re-interpreted it 5.5
    hours off. Marking those values ``+00:00`` keeps the same instant while
    making it host-independent, and stops any not-yet-upgraded reader from
    guessing. Aware values are left alone, so this is idempotent.

    Returns the number of fields rewritten (0 when there was nothing to do).
    """
    from .. import config
    from .json_file import read_json, write_json

    target = Path(path) if path is not None else Path(config.SCHEDULE_FILE)
    with _file_lock(target):
        entries = read_json(target, [])
        if not isinstance(entries, list):
            return 0
        changed = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for field in ("next_due", "paused_until"):
                raw = entry.get(field)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue  # unparseable: leave it for the reader to reject
                if parsed.tzinfo is not None:
                    continue
                entry[field] = parsed.replace(
                    tzinfo=timezone.utc).isoformat(timespec="seconds")
                changed += 1
        if changed:
            write_json(target, entries)
            log.info("schedule.json: %d naive timestamp(s) normalised to UTC", changed)
        return changed
