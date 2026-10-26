"""Scheduled-reports persistence (schedule.json).

Pushed to GitHub with the rest of the state so /schedule add/remove changes
survive redeploys instead of living only in env vars. Each entry:
{"interval_min": int, "commands": [str,...], "chat": str}. Every Telegram user
manages their own entries - a chat only ever sees and changes the rows that
belong to it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as datetime_timezone

from .. import config
from ..core.dates import parse_utc_timestamp
from ..market.hours import market_tz_name, normalise_market
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)
_UNSET = object()


def schedule_at_times(entry: dict) -> tuple[str, ...]:
    """Canonical clock times, accepting legacy run_at and explicit at_times."""
    raw = entry.get("at_times") or entry.get("run_at") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    times = set()
    for value in raw:
        value = str(value).strip()
        if not value:
            continue
        try:
            hour, minute = (int(part) for part in value.split(":"))
            if 0 <= hour < 24 and 0 <= minute < 60:
                value = f"{hour:02d}:{minute:02d}"
        except ValueError:
            pass
        times.add(value)
    return tuple(sorted(times))


def schedule_identity(entry: dict, default_chat="", default_market=None) -> tuple:
    """Shared runtime/persistence identity; mutable due/pause state is excluded."""
    market = normalise_market(entry.get("market") or default_market
                              or config.SCHEDULED_REPORTS_MARKET)
    return (
        str(entry.get("chat") or default_chat or config.SCHEDULED_REPORTS_CHAT
                    or config.TELEGRAM_CHAT_ID or ""),
        tuple(str(command).strip() for command in entry.get("commands") or []
              if str(command).strip()),
        int(entry.get("interval_min") or config.SCHEDULED_REPORTS_INTERVAL_MIN),
        schedule_at_times(entry),
        str(entry.get("timezone") or market_tz_name(market)),
        market,
        str(entry.get("window_start") or "").strip(),
        str(entry.get("window_end") or "").strip(),
    )


def schedule_defaults_enabled(chat_id) -> bool:
    """Explicit clears must not resurrect the owner's environment defaults."""
    from .settings import get_user_settings

    return not get_user_settings(chat_id).get("schedule_defaults_disabled", False)


def _disable_schedule_defaults(chat_id) -> None:
    from .settings import patch_user_settings

    # The settings patch shares the reentrant state lock with schedule writes,
    # so defaults are disabled before the last row disappears.
    patch_user_settings(chat_id, {"schedule_defaults_disabled": True})


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
                "at_times": item.get("at_times"),
                "timezone": item.get("timezone"),
                # Optional market-hours gate: 'in' / 'us' / 'any'.
                "market": item.get("market"),
                # Optional explicit run window "HH:MM" (in the market's tz).
                "window_start": item.get("window_start"),
                "window_end": item.get("window_end"),
                # Pause support (ISO timestamp); the entry is skipped until then.
                "paused_until": item.get("paused_until"),
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


def is_undeliverable_chat(chat) -> bool:
    """True when no Telegram message can ever reach `chat`.

    The Bot API only delivers to numeric chat ids (users/groups) or channel
    usernames where the bot is admin - the bot's OWN username is never a
    valid target, yet it ends up in schedule rows whenever owner-addressed
    flows run with TELEGRAM_CHAT_ID misconfigured as @BotName instead of a
    numeric id. Such rows fail every day with an API error and are invisible
    in the admin Users list, so refuse them at the single save choke point.
    """
    key = str(chat or "").strip()
    if not key:
        return True
    own = str(getattr(config, "BOT_USERNAME", "") or "").strip().lstrip("@").lower()
    return bool(own) and key.lstrip("@").lower() == own


def add_schedule_entry(
    interval_min: int, commands: list[str], chat: str, run_at: str | None = None,
    market: str | None = None, window_start: str | None = None,
    window_end: str | None = None,
    at_times: list[str] | None = None, timezone: str | None = None,
) -> list[dict]:
    """Append a schedule entry, then return the new full schedule.

    run_at is an optional "HH:MM" wall-clock time. When set, the first report
    fires at the next occurrence of that time and then repeats every
    interval_min; without it the entry is plain interval-based (as before).
    market is the optional market-hours gate ('in'/'us'/'any') and
    window_start/window_end an optional explicit run window ("HH:MM") in the
    market's timezone. Raises ValueError for an undeliverable chat id.
    """
    if is_undeliverable_chat(chat):
        raise ValueError(
            f"refusing schedule for {str(chat).strip()!r} - no Telegram message "
            "can reach the bot itself; use a numeric user/group chat id")
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
        if at_times:
            entry["at_times"] = list(at_times)
        if timezone:
            entry["timezone"] = timezone
        if market:
            entry["market"] = str(market).strip().lower()
        if window_start:
            entry["window_start"] = window_start
        if window_end:
            entry["window_end"] = window_end
        current.append(entry)
        write_json(config.SCHEDULE_FILE, current)
    log.info(
        "schedule.json: added entry every %d min%s%s -> %s (chat %s)",
        interval_min, f" at {run_at}" if run_at else "",
        f" market={market}" if market else "",
        ", ".join(commands), chat,
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
            if len(own_entries) == 1:
                _disable_schedule_defaults(chat_id)
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
        _disable_schedule_defaults(chat_id)
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


def set_schedule_next_due(
    chat_id, commands: list[str], interval_min: int, due_ts: float,
    run_at=_UNSET, market=_UNSET, window_start=_UNSET, window_end=_UNSET,
    at_times=_UNSET, timezone=_UNSET,
) -> None:
    """Persist an aware UTC due time using the shared schedule identity.

    Optional identity fields preserve old four-argument callers. Such callers
    update a unique legacy match only: ambiguity must never advance another
    schedule's timer. Explicit None means an unset field, not a wildcard.
    """
    target = {"chat": str(chat_id), "commands": commands, "interval_min": interval_min}
    optional = {"run_at": run_at, "at_times": at_times, "timezone": timezone,
                "market": market, "window_start": window_start, "window_end": window_end}
    target.update({name: value for name, value in optional.items() if value is not _UNSET})
    identity = schedule_identity(target)
    positions = [0, 1, 2]
    if run_at is not _UNSET or at_times is not _UNSET:
        positions.append(3)
    if timezone is not _UNSET:
        positions.append(4)
    if market is not _UNSET:
        positions.append(5)
    if window_start is not _UNSET:
        positions.append(6)
    if window_end is not _UNSET:
        positions.append(7)
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        matches = []
        for entry in current:
            if not isinstance(entry, dict):
                continue
            candidate = schedule_identity(entry)
            if all(candidate[index] == identity[index] for index in positions):
                matches.append(entry)
        if len(matches) == 1:
            matches[0]["next_due"] = datetime.fromtimestamp(
                due_ts, datetime_timezone.utc,
            ).isoformat(timespec="seconds")
            write_json(config.SCHEDULE_FILE, current)
            return
    log.info("schedule.json: %d entries matched next_due update (chat %s)",
             len(matches), chat_id)


def schedule_next_due_ts(entry: dict) -> float | None:
    """Parse an entry's persisted next_due (ISO) back to an epoch timestamp.

    Returns None when unset or malformed so callers fall back to a fresh
    first-run delay.
    """
    raw = (entry or {}).get("next_due")
    if not raw:
        return None
    try:
        parsed = parse_utc_timestamp(raw)
        return parsed.timestamp() if parsed is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def pause_schedule(chat_id, until_ts: float) -> list[dict]:
    """Pause EVERY schedule entry of one chat until an epoch timestamp.

    The pause is persisted (ISO) on each of the chat's rows so it survives
    redeploys; the scheduler skips those entries until the timestamp passes
    (auto-resume). Other users' entries are untouched. Returns the new full
    schedule.
    """
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        key = str(chat_id)
        owner = str(config.TELEGRAM_CHAT_ID or "")
        until_iso = datetime.fromtimestamp(until_ts, datetime_timezone.utc).isoformat(timespec="seconds")
        changed = 0
        for entry in current:
            if not isinstance(entry, dict):
                continue
            if (str(entry.get("chat") or "") == key
                    or (not str(entry.get("chat") or "") and key == owner)):
                entry["paused_until"] = until_iso
                changed += 1
        if changed:
            write_json(config.SCHEDULE_FILE, current)
    log.info("schedule.json: paused chat %s until %s (%d entry(s))", chat_id, until_iso, changed)
    return current


def resume_schedule(chat_id) -> list[dict]:
    """Clear the pause on every schedule entry of one chat (resume now).

    Returns the new full schedule.
    """
    with _lock, _file_lock(config.SCHEDULE_FILE):
        current = read_json(config.SCHEDULE_FILE, [])
        if not isinstance(current, list):
            current = []
        key = str(chat_id)
        owner = str(config.TELEGRAM_CHAT_ID or "")
        changed = 0
        for entry in current:
            if not isinstance(entry, dict):
                continue
            if (str(entry.get("chat") or "") == key
                    or (not str(entry.get("chat") or "") and key == owner)):
                if entry.pop("paused_until", None) is not None:
                    changed += 1
        if changed:
            write_json(config.SCHEDULE_FILE, current)
    log.info("schedule.json: resumed chat %s (%d entry(s))", chat_id, changed)
    return current
