"""Fail-closed JSON persistence and the shared storage/Git transaction lock.

Lock order: state_lock (thread + process), Git lock, per-file lock. Invalid
state is never moved or replaced: repair the canonical file before retrying.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import logging
import math
import os
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)
_lock = threading.RLock()
_local = threading.local()
LOCK_TIMEOUT_SECONDS = 10.0

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt


class InvalidStateError(ValueError):
    """Persisted state is invalid; callers must not substitute empty state."""


class StateLockTimeoutError(TimeoutError):
    """Another state transaction did not release its lock in time."""


class GitStateError(RuntimeError):
    """State access is unsafe until an unfinished Git operation is resolved."""


def strict_json_loads(text: str):
    """Decode JSON without duplicate keys, nonstandard constants or overflow.

    This decoder accepts any JSON root; read_json/write_json restrict their
    persistence API to list/dict roots. Decode failures are ValueError.
    """
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"JSON number overflows a finite float: {value}")
        return result

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                      parse_float=finite_float)


def _timeout_error(lock_path) -> StateLockTimeoutError:
    return StateLockTimeoutError(
        f"Timed out waiting for state lock {lock_path}. Another bot/Git "
        "transaction may be running; wait for it to finish and retry. "
        "Do not delete lock files while a process holds them."
    )


@contextmanager
def _advisory_lock(lock_path: Path, *, timeout: float | None = None):
    timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    key = str(lock_path.resolve())
    if key in held:
        yield
        return
    deadline = time.monotonic() + timeout
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if fcntl is None and handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _timeout_error(lock_path) from error
                time.sleep(min(0.05, remaining))
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@lru_cache(maxsize=16)
def _index_has_conflicts(path: Path, mtime_ns: int, size: int, ctime_ns: int) -> bool:
    """Read index stages only when the index changes (no Git subprocess)."""
    data = path.read_bytes()
    if data[:4] != b"DIRC" or len(data) < 32:
        raise ValueError("invalid Git index header")
    # SHA-256 repositories use wider object IDs, including the checksum.
    if hashlib.sha1(data[:-20]).digest() == data[-20:]:
        oid_size = 20
    elif hashlib.sha256(data[:-32]).digest() == data[-32:]:
        oid_size = 32
    else:
        raise ValueError("invalid Git index checksum")
    version, count = struct.unpack_from("!II", data, 4)
    if version not in (2, 3, 4):
        raise ValueError(f"unsupported Git index version {version}")
    offset, end = 12, len(data) - oid_size
    for _ in range(count):
        start = offset
        flags_offset = start + 40 + oid_size
        if flags_offset + 2 > end:
            raise ValueError("truncated Git index entry")
        flags = struct.unpack_from("!H", data, flags_offset)[0]
        if flags & 0x3000:
            return True
        offset = flags_offset + 2 + (2 if flags & 0x4000 else 0)
        if version == 4:
            # v4 prefix compression: consume the strip-count varint.
            while offset < end and data[offset] & 0x80:
                offset += 1
            offset += 1
        terminator = data.find(b"\0", offset, end)
        if terminator < 0:
            raise ValueError("unterminated Git index path")
        offset = terminator + 1
        if version != 4:
            offset = start + ((offset - start + 7) // 8) * 8
        if offset > end:
            raise ValueError("truncated Git index padding")
    while offset < end:
        if offset + 8 > end:
            raise ValueError("truncated Git index extension")
        signature = data[offset:offset + 4]
        length = struct.unpack_from("!I", data, offset + 4)[0]
        offset += 8
        if offset + length > end:
            raise ValueError("truncated Git index extension payload")
        if signature == b"link":
            shared_id = data[offset:offset + oid_size]
            if len(shared_id) != oid_size:
                raise ValueError("invalid split Git index")
            if any(shared_id):
                shared = path.parent / ("sharedindex." + shared_id.hex())
                stat = shared.stat()
                if _index_has_conflicts(shared, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns):
                    return True
        offset += length
    return False


def ensure_repository_ready() -> None:
    """Reject unfinished Git operations, including conflicts without markers.

    Git integration may also create .git/STATE_CONFLICT while recovering.
    Worktree gitfiles are resolved without invoking Git.
    """
    from .. import config

    git_dir = config.BASE_DIR / ".git"
    try:
        if git_dir.is_file():
            text = git_dir.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: "):
                raise ValueError("invalid .git worktree pointer")
            git_dir = (git_dir.parent / text[8:]).resolve()
        elif not git_dir.exists():
            return
        for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                       "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer", "STATE_CONFLICT"):
            if (git_dir / marker).exists():
                raise GitStateError(
                    f"State access blocked by {git_dir / marker}. Finish or abort "
                    "the Git operation and resolve conflicts before retrying."
                )
        index = git_dir / "index"
        try:
            stat = index.stat()
        except FileNotFoundError:
            return
        if _index_has_conflicts(index, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns):
            raise GitStateError(
                f"State access blocked by unmerged entries in {index}. "
                "Resolve and stage Git conflicts before retrying."
            )
    except (OSError, ValueError) as error:
        raise GitStateError(f"Cannot verify Git state in {git_dir}: {error}") from error


@contextmanager
def state_lock(*, allow_git_operation: bool = False, timeout: float | None = None):
    """Serialize state transactions with Git across threads and processes.

    The Git recovery thread may explicitly bypass repository checks. Its
    nested storage calls inherit the override; other threads never do. Always
    acquire this lock before the Git lock and per-file locks.
    """
    from .. import config

    timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    if not _lock.acquire(timeout=timeout):
        raise _timeout_error(config.BASE_DIR / ".state.lock (thread)")
    previous = getattr(_local, "allow_git_operation", False)
    try:
        with _advisory_lock(config.BASE_DIR / ".state.lock", timeout=timeout):
            _local.allow_git_operation = previous or allow_git_operation
            try:
                if not _local.allow_git_operation:
                    ensure_repository_ready()
                yield
            finally:
                _local.allow_git_operation = previous
    finally:
        _lock.release()


@contextmanager
def _file_lock(path: Path):
    """Reentrant file lock; always acquire the shared state lock first."""
    path = Path(path)
    with state_lock(), _advisory_lock(path.with_suffix(path.suffix + ".lock")):
        yield


def _quarantine(path: Path, error) -> Path:
    """Copy invalid bytes once per content, never remove the canonical file."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    backup = path.with_name(f"{path.name}.corrupt.{digest}.bak")
    try:
        with backup.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if backup.read_bytes() != raw:
            raise OSError(f"Corrupt-state backup does not match {path}: {backup}")
    log.error("Invalid state %s (canonical preserved; backup %s): %s", path, backup, error)
    return backup


def _root(value) -> None:
    if type(value) not in (dict, list):
        raise TypeError("JSON persistence requires a list or dict root")


def read_json(path: Path, default, *, validator=None):
    """Read the exact list/dict root type of default, optionally schema-checking.

    Only a missing file returns a fresh default. Invalid JSON/root/schema
    raises InvalidStateError, with a backup COPY and canonical bytes intact.
    Other I/O errors propagate; they are never mistaken for empty state.
    Validators must raise ValueError on invalid persisted content.
    """
    _root(default)
    path = Path(path)
    with _file_lock(path):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return copy.deepcopy(default)
        except UnicodeError as error:
            _invalid_state(path, error)
        try:
            data = strict_json_loads(text)
            if type(data) is not type(default):
                raise ValueError(f"expected {type(default).__name__}, got {type(data).__name__}")
            if validator is not None:
                validator(data)
        except (ValueError, RecursionError) as error:
            _invalid_state(path, error)
        return data


def _invalid_state(path: Path, error) -> None:
    # A failed backup is an I/O failure, not a verdict on the data: let it
    # propagate so callers never read "could not preserve the bytes" as
    # "invalid state" (and never as empty state either).
    backup = _quarantine(path, error)
    raise InvalidStateError(
        f"Invalid state in {path}: {error}; canonical file left untouched; "
        f"backup: {backup}. Repair or restore the canonical file before retrying."
    ) from error


def write_json(path: Path, data, *, validator=None) -> None:
    """Atomically write a list/dict; refuse to overwrite invalid existing state."""
    _root(data)
    if validator is not None:
        validator(data)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    # Also rejects key coercions that would serialize as duplicate keys.
    strict_json_loads(payload)
    path = Path(path)
    with _file_lock(path):
        read_json(path, type(data)(), validator=validator)
        try:
            if path.read_text(encoding="utf-8") == payload:
                return
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    log.info("Saved %s: %d item(s)", path.name, len(data))
