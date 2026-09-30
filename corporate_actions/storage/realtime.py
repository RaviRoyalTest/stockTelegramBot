"""Centralized persistence for real-time JSON snapshots.

Distinct from the state layer in this package: ``json_file.py`` owns mutable
state files (watchlist, settings, ...) whose newest version *replaces* the old
one. This module owns append-only captures - every write is a NEW timestamped
file, history is never overwritten, and a retention policy prunes old files.

Filename standard (IST timestamps - the market calendar is IST; see core.dates)::

    {data_name}_{YYYY-MM-DD}_{HH-MM-SS-ffffff}.json
    e.g. market_data_2026-09-30_18-42-15-382451.json

The microsecond field plus the unique_path() collision loop makes concurrent
saves safe: even identical ``timestamp=`` values never overwrite each other.
Writes are atomic (tmp file + os.replace, fsync before rename) so a crash
mid-write never leaves a truncated JSON file behind.

Directory layout (date-partitioned so thousands of files stay manageable)::

    data/realtime/{data_name}/{YYYY}/{MM}/{DD}/{file}.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..core.dates import IST, now_ist
from .json_file import _file_lock

__all__ = [
    "REALTIME_ROOT",
    "generate_snapshot_filename",
    "unique_snapshot_path",
    "save_json_snapshot",
    "append_snapshot",
    "cleanup_expired_snapshots",
]

log = logging.getLogger(__name__)

# One lock per process: two threads saving the same data_name at the same
# microsecond must not both claim the same path (the fs-level flock in
# _file_lock already guards cross-process).
_write_lock = threading.Lock()

REALTIME_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "realtime"

_SNAPSHOT_SUFFIX = ".json"

# Data/source name: lowercase slug (letters, digits, hyphen, underscore).
_NAME_RE = re.compile(r"[^a-z0-9_-]+")

# Only files matching the strict snapshot grammar are eligible for retention
# cleanup - one-off files a developer drops into the tree are never touched.
_FILENAME_RE = re.compile(r"^.+_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d{6})?\.json$")

# Manual config override, read at call time (tests can patch the env).
_RETENTION_DAYS_ENV = "JSON_RETENTION_DAYS"


def _normalize_name(data_name: str) -> str:
    """Slug a data/source name so it is always filename-safe."""
    slug = _NAME_RE.sub("-", str(data_name or "").strip().lower()).strip("-")
    if not slug:
        raise ValueError("data_name must contain at least one alphanumeric character")
    return slug


def generate_snapshot_filename(
    data_name: str,
    *,
    timestamp: datetime | None = None,
) -> str:
    """Deterministic snapshot filename: name_YYYY-MM-DD_HH-MM-SS-ffffff.json.

    ``timestamp`` defaults to now in IST (Asia/Kolkata) - the market calendar
    timezone used across the app (see core.dates). Naive datetimes are assumed
    to be IST (a bare ``datetime.now()`` on a UTC host would otherwise silently
    misdate files); aware datetimes are converted.
    """
    moment = timestamp or now_ist()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    else:
        moment = moment.astimezone(IST)
    safe_name = _normalize_name(data_name)
    return (
        f"{safe_name}_{moment:%Y-%m-%d_%H-%M-%S-%f}{_SNAPSHOT_SUFFIX}"
    )


def unique_snapshot_path(
    data_name: str,
    *,
    timestamp: datetime | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Date-partitioned directory + a filename no existing file claims.

    Microsecond precision makes collisions vanishingly rare; the loop then
    guarantees uniqueness even under concurrency or an injected timestamp.
    """
    directory = (base_dir or REALTIME_ROOT) / _normalize_name(data_name) / (
        (timestamp or now_ist()).strftime("%Y/%m/%d")
    )
    candidate = directory / generate_snapshot_filename(data_name, timestamp=timestamp)
    counter = 1
    while candidate.exists():
        candidate = directory / (
            f"{_normalize_name(data_name)}_"
            f"{(timestamp or now_ist()):%Y-%m-%d_%H-%M-%S-%f}-{counter}{_SNAPSHOT_SUFFIX}"
        )
        counter += 1
    return candidate


def _json_default(value):
    """Central JSON fallback: datetimes -> ISO 8601, Decimal -> float.

    Anything else intentionally raises TypeError - unknown objects must fail
    loudly at the persistence boundary instead of being silently str()'d into
    meaningless payloads ("<object at 0x...>").
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)


def _dump_line(data) -> str:
    """Compact single-line dump (JSONL records must never span lines)."""
    return json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), default=_json_default
    )


def save_json_snapshot(
    data_name: str,
    data,
    *,
    source: str | None = None,
    metadata: dict | None = None,
    timestamp: datetime | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Persist one append-only JSON snapshot; return the final path.

    Atomic write under a per-path lock, a ``metadata`` block carrying the
    capture timestamp/timezone/source (requirement: keep the capture time
    inside the file, not only in the name), INFO log on success, and a
    raised (never swallowed) exception on failure.
    """
    safe_name = _normalize_name(data_name)
    started = time.perf_counter()
    record_count = len(data) if isinstance(data, (list, dict, str)) else None
    path: Path | None = None
    try:
        path = unique_snapshot_path(
            safe_name, timestamp=timestamp, base_dir=base_dir
        )

        envelope = dict(metadata or {})
        moment = timestamp or now_ist()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=IST)
        envelope.setdefault(
            "captured_at", moment.isoformat(timespec="microseconds")
        )
        envelope.setdefault("timezone", "Asia/Kolkata")
        if source:
            envelope.setdefault("source", source)

        payload = {"metadata": envelope, "data": data} if envelope else data
        body = _dump(payload)

        with _write_lock, _file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as file_handle:
                    file_handle.write(body)
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                os.replace(temp_path, path)
            except OSError:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
    except (OSError, TypeError, ValueError):
        log.exception(
            "Failed to save JSON snapshot: data_name=%s path=%s", safe_name, path or "<unresolved>"
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    size_bytes = path.stat().st_size
    log.info(
        "JSON snapshot saved: data_name=%s records=%s size=%dB duration=%.1fms path=%s",
        safe_name,
        record_count if record_count is not None else "n/a",
        size_bytes,
        duration_ms,
        path,
    )
    return path


def append_snapshot(
    data_name: str,
    record,
    *,
    timestamp: datetime | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Append one record to today's JSONL snapshot series for ``data_name``.

    High-frequency captures (one row per poll/tick) should not become one file
    per row: this writes ``{name}_YYYY-MM-DD.jsonl`` - one JSON object per
    line, opened in append mode and closed immediately. Each line carries its
    own captured_at. Failure raises after logging; the caller decides whether
    that is fatal.
    """
    safe_name = _normalize_name(data_name)
    moment = timestamp or now_ist()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    else:
        moment = moment.astimezone(IST)
    started = time.perf_counter()
    path: Path | None = None
    try:
        directory = (base_dir or REALTIME_ROOT) / safe_name
        path = directory / f"{safe_name}_{moment:%Y-%m-%d}.jsonl"
        line = _dump_line(
            {
                "captured_at": moment.isoformat(timespec="microseconds"),
                "record": record,
            }
        )
        directory.mkdir(parents=True, exist_ok=True)
        with _file_lock(path), open(path, "a", encoding="utf-8") as file_handle:
            file_handle.write(line + "\n")
    except (OSError, TypeError, ValueError):
        log.exception(
            "Failed to append JSONL snapshot: data_name=%s path=%s", safe_name, path or "<unresolved>"
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "JSONL snapshot appended: data_name=%s duration=%.1fms path=%s",
        safe_name,
        duration_ms,
        path,
    )
    return path


def cleanup_expired_snapshots(
    *,
    base_dir: Path | None = None,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Delete snapshot files older than the retention window.

    Disabled by default: pass ``retention_days`` explicitly or set the
    ``JSON_RETENTION_DAYS`` env var (0 or invalid disables). Only files whose
    names match the snapshot grammar are eligible - hand-placed files are
    never touched. Returns (removed_count, bytes_reclaimed); logs every
    removal so cleanup is auditable.
    """
    if retention_days is None:
        raw = os.getenv(_RETENTION_DAYS_ENV, "").strip()
        try:
            retention_days = int(raw) if raw else 0
        except ValueError:
            log.warning("Invalid %s=%r - retention disabled", _RETENTION_DAYS_ENV, raw)
            retention_days = 0
    if retention_days <= 0:
        return (0, 0)

    root = base_dir or REALTIME_ROOT
    moment = now or now_ist()
    cutoff = moment.timestamp() - retention_days * 86400
    removed = 0
    reclaimed = 0
    try:
        candidates = list(root.rglob("*.json")) + list(root.rglob("*.jsonl"))
    except OSError:
        log.exception("Retention cleanup could not scan %s", root)
        return (0, 0)
    for path in candidates:
        if not _FILENAME_RE.match(path.name):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                reclaimed += size
                log.info(
                    "Retention removed snapshot: path=%s size=%dB", path, size
                )
        except OSError:
            log.exception("Retention cleanup failed for %s", path)
    if removed or reclaimed:
        log.info(
            "Retention cleanup complete: removed=%d reclaimed=%dB retention_days=%d",
            removed,
            reclaimed,
            retention_days,
        )
    return (removed, reclaimed)
