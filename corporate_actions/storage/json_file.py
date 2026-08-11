"""Base layer for every JSON state file: atomic writes + cross-process locks.

The always-on bot server and the GitHub Actions cron are separate processes
that can write the same state files. Without an OS-level lock, a concurrent
read-modify-write silently drops one side's changes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()

try:
    import fcntl
except ImportError:  # non-POSIX (e.g. Windows) - fall back to in-process lock only
    fcntl = None


@contextmanager
def _file_lock(path: Path):
    """Cross-process advisory lock for a JSON state file."""
    file_handle = None
    locked = False
    if fcntl is not None:
        try:
            lock_path = path.with_suffix(path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            file_handle = open(lock_path, "a+")
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError:
            file_handle = None
            locked = False
    try:
        yield
    finally:
        if locked and file_handle is not None:
            try:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            file_handle.close()


def read_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as file_handle:
                data = json.load(file_handle)
                if isinstance(data, (list, dict)):
                    return data
    except (OSError, ValueError) as error:
        log.warning("Failed to read %s: %s", path.name, error)
    return default


def write_json(path: Path, data) -> None:
    """Write data to disk atomically, logging only when content changed.

    The write goes to a temp file in the same directory followed by an atomic
    os.replace(), so a crash or concurrent process never leaves a truncated
    JSON file behind. Skipping identical writes keeps the logs quiet - the
    Streamlit UI persists the watchlist on every rerun, and a rewrite with the
    same content would otherwise spam "Saved ..." lines and touch the file
    needlessly.
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == payload:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(payload)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, path)
    except OSError:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    if isinstance(data, list):
        log.info("Saved %s: %d item(s)", path.name, len(data))
    elif isinstance(data, dict):
        log.info("Saved %s: %d user(s)", path.name, len(data))
