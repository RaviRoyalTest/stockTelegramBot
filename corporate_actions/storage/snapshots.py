"""Recorded market snapshots (data/snapshots.json + data/snapshots/).

The last write lives in data/snapshots.json for legacy load_snapshots()
callers. load_snapshots(market) also consults the archive so one market's
record never hides the other market's latest session.

Every record is ALSO archived under its session date+market,
``data/snapshots/YYYY-MM-DD-<market>.json``, so history accumulates instead
of each record clobbering the last. That makes the recorded details
REUSABLE: any past session recorded once can be reopened later at zero
cost (web Sessions history picker), and a future feature can serve any
archived day without re-fetching anything.
"""
from __future__ import annotations

import logging
from datetime import date

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def _history_dir():
    """The archive directory, derived from SNAPSHOT_FILE at call time.

    Derived (not a config constant) so tests that patch SNAPSHOT_FILE into
    a temp dir keep their archive isolated too.
    """
    return config.SNAPSHOT_FILE.parent / "snapshots"


def _session_key(session) -> str | None:
    """Accept only canonical calendar dates, never arbitrary path components."""
    if not isinstance(session, str):
        return None
    try:
        return session if date.fromisoformat(session).isoformat() == session else None
    except ValueError:
        return None


def _market_key(market) -> str | None:
    key = str(market).strip().lower()
    return key if key in ("in", "us") else None


def _dated_path(session: str, market: str):
    from pathlib import Path

    day = _session_key(session)
    market_key = _market_key(market)
    if day is None or market_key is None:
        raise ValueError("Expected an ISO session date and market 'in' or 'us'")
    return Path(_history_dir()) / f"{day}-{market_key}.json"


def _capture_versioned(doc: dict) -> None:
    """Best-effort timestamped audit capture of one snapshot record.

    The session archive keeps its reuse contract (one file per
    session+market, fresher record wins); this additionally lands an
    append-only capture
    ``data/realtime/market_snapshot/{Y}/{M}/{D}/market_snapshot_<IST-timestamp>.json``
    per RECORD so re-records of the same session are versioned, never
    overwritten. Never raises (logged as a warning on failure); the
    directory derives from SNAPSHOT_FILE so patched tests stay hermetic.
    """
    try:
        from .realtime import save_json_snapshot

        save_json_snapshot(
            "market_snapshot",
            doc,
            source="session-record",
            base_dir=config.SNAPSHOT_FILE.parent / "realtime",
        )
    except Exception as error:
        log.warning("snapshot versioned capture skipped: %s", error)


def archive_snapshot(doc: dict) -> str | None:
    """Archive one snapshot doc under its date+market, atomically.

    Never raises. Returns the archived file's date key ("2026-09-29-in")
    or None when the doc carries no usable session date. An existing file
    for the same day+market is overwritten (same session, fresher record).
    """
    try:
        if not isinstance(doc, dict) or not doc:
            return None
        session = _session_key(doc.get("session"))
        if session is None:
            return None
        market = str(doc.get("market") or "in")
        path = _dated_path(session, market)
        with _lock, _file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, doc)
        _capture_versioned(doc)
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
        market_key = _market_key(market) if market is not None else None
        if market is not None and market_key is None:
            return []
        days = []
        for path in directory.glob("*.json"):
            if not path.is_file():
                continue
            day, _, archived_market = path.stem.rpartition("-")
            if _market_key(archived_market) is None:
                continue
            if market_key and archived_market != market_key:
                continue
            if _session_key(day) is not None:
                days.append(day)
        return sorted(set(days))
    except Exception:
        return []


def load_snapshot_archive(session: str, market: str | None = None) -> dict:
    """One archived record for a session date ({} when never recorded).

    With no market hint the India file is preferred, then US - the date is
    what the user asks for. Invalid dates/markets and corrupt docs return {}.
    """
    requested = _session_key(session)
    requested_market = _market_key(market) if market is not None else None
    if requested is None or (market is not None and requested_market is None):
        return {}
    for market_key in ("in", "us"):
        if requested_market and requested_market != market_key:
            continue
        try:
            path = _dated_path(requested, market_key)
            if not path.is_file():
                continue
            with _lock, _file_lock(path):
                data = read_json(path, {})
            if isinstance(data, dict) and data.get("session") == requested \
                    and _market_key(data.get("market", "in")) == market_key:
                return data
        except (OSError, ValueError):
            continue
    return {}


def load_snapshots(market: str | None = None) -> dict:
    """Latest record, optionally scoped to a market via the session archive.

    No argument preserves the legacy single-file read. Market-aware callers
    get the newest valid session across that file and their market's archive;
    a corrupt newest archive is skipped rather than hiding older good data.
    """
    key = _market_key(market) if market is not None else None
    if market is not None and key is None:
        return {}
    try:
        with _lock, _file_lock(config.SNAPSHOT_FILE):
            data = read_json(config.SNAPSHOT_FILE, {})
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    if market is None:
        return data
    candidate = data if _market_key(data.get("market", "in")) == key \
        and _session_key(data.get("session")) else {}
    for session in reversed(list_snapshot_dates(key)):
        if candidate and session <= candidate["session"]:
            break
        archived = load_snapshot_archive(session, key)
        if archived:
            return archived
    return candidate


def save_snapshots(doc: dict) -> None:
    """Persist one snapshot doc, atomically, and archive it by date."""
    with _lock, _file_lock(config.SNAPSHOT_FILE):
        write_json(config.SNAPSHOT_FILE, doc or {})
    archive_snapshot(doc if isinstance(doc, dict) else {})
