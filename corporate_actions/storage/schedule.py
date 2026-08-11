"""Scheduled-reports persistence (schedule.json).

Pushed to GitHub with the rest of the state so /schedule add/remove changes
survive redeploys instead of living only in env vars. Each entry:
{"interval_min": int, "commands": [str,...], "chat": str}. Every Telegram user
manages their own entries - a chat only ever sees and changes the rows that
belong to it.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def load_schedule() -> list[dict]:
    """Return list of {'interval_min', 'commands', 'chat'} schedule entries."""
    with _lock:
        data = read_json(config.SCHEDULE_FILE, [])
    cleaned = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        interval = item.get("interval_min")
        commands = item.get("commands")
        if isinstance(interval, int) and interval >= 1 and isinstance(commands, list):
            cleaned.append({
                "interval_min": interval,
                "commands": [str(command).strip() for command in commands if str(command).strip()],
                "chat": str(item.get("chat", "") or ""),
                # Persisted next-run timestamp (ISO) - kept so the cadence
                # survives redeploys (see set_schedule_next_due).
                "next_due": item.get("next_due"),
                # Optional wall-clock "HH:MM" (IST) - first run at that time.
                "run_at": item.get("run_at"),
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
        entry for entry in load_schedule()
        if str(entry.get("chat") or "") == key
        or (not str(entry.get("chat") or "") and key == owner)
    ]


def save_schedule(entries: list[dict]) -> None:
    with _lock, _file_lock(config.SCHEDULE_FILE):
        write_json(config.SCHEDULE_FILE, entries)
    log.info("schedule.json: %d scheduled report entry(s)", len(entries))


def add_schedule_entry(
    interval_min: int, commands: list[str], chat: str, run_at: str | None = None
) -> list[dict]:
    """Append a schedule entry, then return the new full schedule.

    run_at is an optional "HH:MM" wall-clock time (IST). When set, the first
    report fires at the next occurrence of that time and then repeats every
    interval_min; without it the entry is plain interval-based (as before).
    """
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        entry = {
            "interval_min": interval_min,
            "commands": [command for command in commands if command.strip()],
            "chat": str(chat),
        }
        if run_at:
            entry["run_at"] = run_at
        current.append(entry)
        write_json(config.SCHEDULE_FILE, current)
    log.info(
        "schedule.json: added entry every %d min%s -> %s (chat %s)",
        interval_min, f" at {run_at}" if run_at else "", ", ".join(commands), chat,
    )
    return current


def remove_schedule_entry(chat_id, index: int) -> list[dict]:
    """Remove the index-th (0-based) schedule entry belonging to chat_id.

    Indexes are relative to that chat's own entries (as shown by /schedule),
    so one user removing theirs never touches another user's rows. Returns
    the new full schedule.
    """
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        key = str(chat_id)
        owner = str(config.TELEGRAM_CHAT_ID or "")
        own_entries = [
            index for index, entry in enumerate(current)
            if str(entry.get("chat") or "") == key
            or (not str(entry.get("chat") or "") and key == owner)
        ]
        removed = None
        if 0 <= index < len(own_entries):
            removed = current.pop(own_entries[index])
            write_json(config.SCHEDULE_FILE, current)
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
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        key = str(chat_id)
        owner = str(config.TELEGRAM_CHAT_ID or "")
        kept = [
            entry for entry in current
            if not (str(entry.get("chat") or "") == key
                    or (not str(entry.get("chat") or "") and key == owner))
        ]
        write_json(config.SCHEDULE_FILE, kept)
    log.info(
        "schedule.json: cleared chat %s (%d entry(s) removed)",
        chat_id, len(current) - len(kept),
    )
    return kept


def set_schedule_next_due(chat_id, commands: list[str], interval_min: int, due_ts: float) -> None:
    """Persist the next-run timestamp on the matching schedule entry.

    Stored in schedule.json so the next-run time survives redeploys - after
    a restart the scheduler resumes the same cadence instead of resetting to
    "boot + 1 minute". Entries are matched by (chat, commands, interval),
    the same identity the scheduler keys its in-memory due-times on.
    """
    key = str(chat_id)
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        for entry in current:
            if not isinstance(entry, dict):
                continue
            if (str(entry.get("chat") or "") == key
                    and [command for command in entry.get("commands") or [] if command.strip()] == [command for command in commands if command.strip()]
                    and int(entry.get("interval_min") or 0) == int(interval_min)):
                entry["next_due"] = datetime.fromtimestamp(due_ts).isoformat(timespec="seconds")
                write_json(config.SCHEDULE_FILE, current)
                log.info(
                    "schedule.json: next_due for chat %s set to %s",
                    chat_id, entry["next_due"],
                )
                return
    log.info("schedule.json: no entry matched for next_due update (chat %s)", chat_id)


def schedule_next_due_ts(entry: dict) -> float | None:
    """Parse an entry's persisted next_due (ISO) back to an epoch timestamp.

    Returns None when unset or malformed so callers fall back to a fresh
    first-run delay.
    """
    raw = (entry or {}).get("next_due")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None
