"""Persistent storage for the selected watchlist and de-duplication cache."""
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()

try:
    import fcntl
except ImportError:  # non-POSIX (e.g. Windows) - fall back to in-process lock only
    fcntl = None


@contextmanager
def _file_lock(path: Path):
    """Cross-process advisory lock for a JSON state file.

    The always-on bot server and the GitHub Actions cron are separate
    processes that can write the same state files. Without an OS-level lock,
    a concurrent read-modify-write silently drops one side's changes.
    """
    fh = None
    locked = False
    if fcntl is not None:
        try:
            lock_path = path.with_suffix(path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError:
            fh = None
            locked = False
    try:
        yield
    finally:
        if locked and fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


def _read_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data
    except (OSError, ValueError) as exc:
        log.warning("Failed to read %s: %s", path.name, exc)
    return default


def _write_json(path: Path, data) -> None:
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if isinstance(data, list):
        log.info("Saved %s: %d item(s)", path.name, len(data))
    elif isinstance(data, dict):
        log.info("Saved %s: %d user(s)", path.name, len(data))


# ----------------------------------------------------------------- watchlist

def load_watchlist() -> list[dict]:
    """Return list of {'symbol', 'company', 'exchange'}."""
    with _lock:
        data = _read_json(config.WATCHLIST_FILE, [])
    return [item for item in data if isinstance(item, dict)]


def save_watchlist(watchlist: list[dict]) -> None:
    with _lock:
        _write_json(config.WATCHLIST_FILE, watchlist)


def _watchlist_key(item: dict) -> tuple:
    return (item.get("exchange", "").upper(), item.get("symbol", "").upper())


def add_to_watchlist(items: list[dict]) -> list[dict]:
    """Add entries, de-duplicating on (exchange, symbol). Returns new list."""
    with _lock, _file_lock(config.WATCHLIST_FILE):
        current = _read_json(config.WATCHLIST_FILE, [])
        before = len(current)
        seen = {_watchlist_key(i) for i in current}
        added_keys, skipped = [], []
        for item in items:
            key = _watchlist_key(item)
            if key not in seen and item.get("symbol"):
                current.append(item)
                seen.add(key)
                added_keys.append(key)
            elif key in seen:
                skipped.append(key)
        _write_json(config.WATCHLIST_FILE, current)
    log.info(
        "watchlist.json: %d -> %d item(s) | added: %s | skipped (already present): %s",
        before,
        len(current),
        ", ".join(f"{k[0]}:{k[1]}" for k in added_keys) or "none",
        ", ".join(f"{k[0]}:{k[1]}" for k in skipped) or "none",
    )
    return current


def remove_from_watchlist(symbol: str, exchange: str) -> list[dict]:
    with _lock, _file_lock(config.WATCHLIST_FILE):
        current = _read_json(config.WATCHLIST_FILE, [])
        before = len(current)
        kept = [
            i
            for i in current
            if not (i.get("symbol", "").upper() == symbol.upper()
                    and i.get("exchange", "").upper() == exchange.upper())
        ]
        _write_json(config.WATCHLIST_FILE, kept)
    log.info(
        "watchlist.json: %d -> %d item(s) | removed %s:%s",
        before, len(kept), exchange.upper(), symbol.upper(),
    )
    return kept


# ------------------------------------------------------------------ users
# The owner's list is the app watchlist (watchlist.json). Other Telegram users
# each get their own subscription (subscriptions.json) and receive alerts in
# their own chat.

def is_owner(chat_id) -> bool:
    return str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def load_subscriptions() -> dict:
    """Return {chat_id(str): [item, ...]} for non-owner users."""
    with _lock:
        data = _read_json(config.SUBSCRIPTIONS_FILE, {})
    cleaned = {}
    for k, v in data.items():
        if isinstance(v, list):
            cleaned[str(k)] = [i for i in v if isinstance(i, dict)]
    return cleaned


def get_user_list(chat_id) -> list:
    if is_owner(chat_id):
        return load_watchlist()
    subs = load_subscriptions()
    return subs.get(str(chat_id), [])


def add_to_user_list(chat_id, item: dict) -> list:
    if is_owner(chat_id):
        return add_to_watchlist([item])
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = _read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        current = subs.get(key, [])
        before = len(current)
        seen = {_watchlist_key(i) for i in current}
        added = item.get("symbol") and _watchlist_key(item) not in seen
        if added:
            current.append(item)
        subs[key] = current
        _write_json(config.SUBSCRIPTIONS_FILE, subs)
    log.info(
        "subscriptions.json: chat %s %d -> %d item(s) | %s %s:%s",
        key, before, len(current),
        "added" if added else "skipped (already present)",
        item.get("exchange", "").upper(),
        item.get("symbol", "").upper(),
    )
    return current


def remove_from_user_list(chat_id, symbol: str, exchange: str) -> list:
    if is_owner(chat_id):
        return remove_from_watchlist(symbol, exchange)
    with _lock, _file_lock(config.SUBSCRIPTIONS_FILE):
        subs = _read_json(config.SUBSCRIPTIONS_FILE, {})
        key = str(chat_id)
        before = len(subs.get(key, []))
        current = [
            i for i in subs.get(key, [])
            if not (i.get("symbol", "").upper() == symbol.upper()
                    and i.get("exchange", "").upper() == exchange.upper())
        ]
        subs[key] = current
        _write_json(config.SUBSCRIPTIONS_FILE, subs)
    log.info(
        "subscriptions.json: chat %s %d -> %d item(s) | removed %s:%s",
        key, before, len(current), exchange.upper(), symbol.upper(),
    )
    return current


# ------------------------------------------------------------------ settings
# Per-user alert preferences (action-type filters, price-move threshold) live in
# settings.json so they survive restarts and GitHub Actions re-runs.


def load_settings() -> dict:
    """Return {chat_id(str): settings dict} for all users."""
    with _lock:
        data = _read_json(config.SETTINGS_FILE, {})
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def get_user_settings(chat_id) -> dict:
    """Return the settings dict for a chat (empty dict when none stored)."""
    return load_settings().get(str(chat_id), {})


def save_user_settings(chat_id, settings: dict) -> None:
    """Merge/replace a chat's settings dict."""
    with _lock, _file_lock(config.SETTINGS_FILE):
        current = _read_json(config.SETTINGS_FILE, {})
        current[str(chat_id)] = settings
        _write_json(config.SETTINGS_FILE, current)
    log.info("settings.json: chat %s settings = %s", chat_id, settings)


# ---------------------------------------------------------------- seen cache

def load_seen() -> set:
    """Return set of event keys already notified (survives restarts)."""
    with _lock, _file_lock(config.SEEN_FILE):
        data = _read_json(config.SEEN_FILE, [])
    return set(data) if isinstance(data, list) else set()


def save_seen(keys: set) -> None:
    with _lock, _file_lock(config.SEEN_FILE):
        _write_json(config.SEEN_FILE, sorted(keys))


# --------------------------------------------------------------- schedule
# Scheduled reports live in schedule.json (pushed to GitHub with the rest of
# the state) so /schedule add/remove changes survive redeploys instead of living
# only in env vars. Each entry: {"interval_min": int, "commands": [str,...],
# "chat": str}. Every Telegram user manages their own entries - a chat only
# ever sees and changes the rows that belong to it.

def load_schedule() -> list[dict]:
    """Return list of {'interval_min', 'commands', 'chat'} schedule entries."""
    with _lock:
        data = _read_json(config.SCHEDULE_FILE, [])
    cleaned = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        interval = item.get("interval_min")
        commands = item.get("commands")
        if isinstance(interval, int) and interval >= 1 and isinstance(commands, list):
            cleaned.append({
                "interval_min": interval,
                "commands": [str(c).strip() for c in commands if str(c).strip()],
                "chat": str(item.get("chat", "") or ""),
            })
    return cleaned


def load_schedule_for(chat_id) -> list[dict]:
    """Return the schedule entries that belong to one chat.

    Legacy rows with an empty chat field count as the owner's (they were
    created before per-user schedules, when only the owner could schedule).
    """
    key = str(chat_id)
    owner = str(config.TELEGRAM_CHAT_ID or "")
    return [
        e for e in load_schedule()
        if str(e.get("chat") or "") == key
        or (not str(e.get("chat") or "") and key == owner)
    ]


def save_schedule(entries: list[dict]) -> None:
    with _lock, _file_lock(config.SCHEDULE_FILE):
        _write_json(config.SCHEDULE_FILE, entries)
    log.info("schedule.json: %d scheduled report entry(s)", len(entries))


def add_schedule_entry(interval_min: int, commands: list[str], chat: str) -> list[dict]:
    """Append a schedule entry, then return the new full schedule."""
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = _read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        current.append({
            "interval_min": interval_min,
            "commands": [c for c in commands if c.strip()],
            "chat": str(chat),
        })
        _write_json(config.SCHEDULE_FILE, current)
    log.info(
        "schedule.json: added entry every %d min -> %s (chat %s)",
        interval_min, ", ".join(commands), chat,
    )
    return current


def remove_schedule_entry(chat_id, index: int) -> list[dict]:
    """Remove the index-th (0-based) schedule entry belonging to chat_id.

    Indexes are relative to that chat's own entries (as shown by /schedule),
    so one user removing theirs never touches another user's rows. Returns
    the new full schedule.
    """
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = _read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        key = str(chat_id)
        owner = str(config.TELEGRAM_CHAT_ID or "")
        own = [
            i for i, e in enumerate(current)
            if str(e.get("chat") or "") == key
            or (not str(e.get("chat") or "") and key == owner)
        ]
        removed = None
        if 0 <= index < len(own):
            removed = current.pop(own[index])
            _write_json(config.SCHEDULE_FILE, current)
    log.info(
        "schedule.json: removed chat %s entry %d -> %s",
        chat_id, index, removed if removed else "not found",
    )
    return current


def clear_schedule(chat_id) -> list[dict]:
    """Remove every schedule entry belonging to chat_id only.

    Other users' rows stay untouched. Returns the new full schedule.
    """
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = _read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        key = str(chat_id)
        owner = str(config.TELEGRAM_CHAT_ID or "")
        kept = [
            e for e in current
            if not (str(e.get("chat") or "") == key
                    or (not str(e.get("chat") or "") and key == owner))
        ]
        _write_json(config.SCHEDULE_FILE, kept)
    log.info(
        "schedule.json: cleared chat %s (%d entry(s) removed)",
        chat_id, len(current) - len(kept),
    )
    return kept
