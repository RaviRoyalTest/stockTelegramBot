"""Recorded market snapshots (data/snapshots.json + data/snapshots/).

The latest record lives in the single file data/snapshots.json exactly as
before - every existing caller (Sessions tab, daily mail, poller) keeps
reading that with load_snapshots() / save_snapshots().

NEW: every record is ALSO archived under its session date+market,
``data/snapshots/YYYY-MM-DD-<market>.json``, so history accumulates instead
of each record clobbering the last. That makes the recorded details
REUSABLE: any past session recorded once can be reopened later at zero
cost (web Sessions history picker), and a future feature can serve any
archived day without re-fetching anything.
"""
from __future__ import annotations

import logging

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def _history_dir():
    """The archive directory, derived from SNAPSHOT_FILE at call time.

    Derived (not a config constant) so tests that patch SNAPSHOT_FILE into
    a temp dir keep their archive isolated too.
    """
    return config.SNAPSHOT_FILE.parent / "snapshots"


def _dated_path(session: str, market: str):
    from pathlib import Path

    day = str(session or "unknown").strip() or "unknown"
    market_key = "us" if str(market or "").strip().lower() == "us" else "in"
    return Path(_history_dir()) / f"{day}-{market_key}.json"


def archive_snapshot(doc: dict) -> str | None:
    """Archive one snapshot doc under its date+market, atomically.

    Never raises. Returns the archived file's date key ("2026-09-29-in")
    or None when the doc carries no usable session date. An existing file
    for the same day+market is overwritten (same session, fresher record).
    """
    try:
        if not isinstance(doc, dict) or not doc:
            return None
        session = str(doc.get("session") or "").strip()
        if not session:
            return None
        market = str(doc.get("market") or "in")
        path = _dated_path(session, market)
        with _lock, _file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, doc)
        return path.stem
    except Exception as error:
        log.warning("snapshot archive skipped: %s", error)
        return None


def list_snapshot_dates(market: str | None = None) -> list[str]:
    """Archived session dates (oldest first); optional market filter."""
    try:
        directory = _history_dir()
        if not directory.is_dir():
            return []
        suffix = "-us" if str(market or "").lower() == "us" else \
            ("-in" if str(market or "").lower() == "in" else None)
        days = []
        for path in directory.glob("*.json"):
            if not path.is_file():
                continue
            stem = path.stem  # YYYY-MM-DD-<market>
            if suffix and not stem.endswith(suffix):
                continue
            day = stem.rsplit("-", 1)[0]
            if len(day) == 10 and day[4] == "-" and day[7] == "-":
                days.append(day)
        return sorted(set(days))
    except Exception:
        return []


def load_snapshot_archive(session: str, market: str | None = None) -> dict:
    """One archived record for a session date ({} when never recorded).

    With no market hint the India file is preferred, then US - the date is
    what the user asks for; both markets' files for one day are rare.
    """
    requested = str(session or "").strip()
    if not requested:
        return {}
    for market_key in ("in", "us"):
        if market and str(market).lower() != market_key:
            continue
        path = _dated_path(requested, market_key)
        if not path.is_file():
            continue
        with _lock, _file_lock(path):
            data = read_json(path, {})
        if isinstance(data, dict) and data:
            return data
    return {}


def load_snapshots() -> dict:
    """Return the recorded snapshot doc ({} when never recorded)."""
    with _lock, _file_lock(config.SNAPSHOT_FILE):
        data = read_json(config.SNAPSHOT_FILE, {})
    return data if isinstance(data, dict) else {}


def save_snapshots(doc: dict) -> None:
    """Persist one snapshot doc, atomically, and archive it by date."""
    with _lock, _file_lock(config.SNAPSHOT_FILE):
        write_json(config.SNAPSHOT_FILE, doc or {})
    archive_snapshot(doc if isinstance(doc, dict) else {})
