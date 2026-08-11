"""Scheduled-reports loop (single responsibility).

Owns the daemon thread that runs each user's scheduled commands (/schedule
entries) on their own timer. The command runner is injected as a parameter
(dependency inversion) so this module never imports run_bot - run_bot passes
its handle_command in when it starts the loop. This also lets the loop be
tested in isolation.

Every scheduled run is attributed: the runner receives a source label that
names the schedule entry, its cadence, the command and the watchlist the
results relate to - so an automatic report is never anonymous.
"""
import html
import logging
import re
import threading
import time

from . import config, storage
from .formatting.schedule import format_interval

log = logging.getLogger(__name__)


def next_at_ist(hhmm: str) -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in IST.

    Returns None when the string is not a valid HH:MM. Used by the schedule
    so a report can be tied to an exact clock time (e.g. run at 09:15 IST)
    instead of only an interval - and it lands on that minute regardless of
    the host's timezone.
    """
    import datetime as _datetime

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        from zoneinfo import ZoneInfo
        timezone = ZoneInfo("Asia/Kolkata")
    except Exception:
        timezone = None
    now_utc = _datetime.datetime.now(_datetime.timezone.utc)
    if timezone is None:
        now_local = _datetime.datetime.now()
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += _datetime.timedelta(days=1)
        return candidate.timestamp()
    now_ist = now_utc.astimezone(timezone)
    candidate = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_ist:
        candidate += _datetime.timedelta(days=1)
    return candidate.timestamp()


def schedule_source_label(entry: dict, chat, command: str) -> str:
    """HTML banner naming the schedule entry that produced this report.

    Sent before the scheduled command runs so the user always knows which
    scheduled task (and which watchlist) the result belongs to: the entry's
    cadence, the exact command and the chat's watchlist location.
    """
    when = format_interval(entry.get("interval_min") or config.SCHEDULED_REPORTS_INTERVAL_MIN)
    if entry.get("run_at"):
        when += f" at {entry['run_at']} IST"
    origin = "env default" if entry.get("default") else "your /schedule entry"
    where = storage.list_location(chat)
    return (
        f"\U0001F4C5 <b>Scheduled report</b> \u00b7 {origin} \u00b7 {when}\n"
        f"Command: <code>{html.escape(command)}</code>\n"
        f"Source list: <code>{html.escape(where)}</code> \u2014 the results "
        "below come from this scheduled task."
    )


def schedule_entries_with_defaults(default_chat: str) -> list[dict]:
    """schedule.json entries plus the owner's env-default report.

    The env defaults (SCHEDULED_COMMANDS) keep running for the owner chat
    until the owner adds their own file entries. Subscribers adding their own
    entries must never suppress those defaults - this guarantees one user's
    schedule never disturbs another's.
    """
    entries = storage.load_schedule()
    commands = [command for command in config.SCHEDULED_COMMANDS if command.strip()]
    if commands:
        owner_has_entries = any(
            str(entry.get("chat") or default_chat) == str(default_chat)
            for entry in entries
        )
        if not owner_has_entries:
            entries = entries + [{
                "interval_min": config.SCHEDULED_REPORTS_INTERVAL_MIN,
                "commands": commands,
                "chat": default_chat,
                # Marker so the label can say 'env defaults' instead of
                # pretending this synthetic entry came from /schedule.
                "default": True,
            }]
    return entries


def start_scheduled_reports(run_command) -> None:
    """Run scheduled reports to EACH user's own chat on a timer (daemon thread).

    `run_command(chat_id, command_text)` executes one scheduled command - it
    is injected so this module has no dependency on run_bot (no circular
    imports). Only the always-on server runs this (PROCESS_COMMANDS=true);
    the GitHub Actions cron skips it so scans are never sent twice. The first
    report fires a short while after startup so the server has finished
    booting before the scans hit the data feeds.

    Entries come from schedule.json (manageable from Telegram with /schedule)
    and every entry is delivered to the chat that created it - schedules are
    fully per-user. When the owner has no file entries the env-var defaults
    (SCHEDULED_COMMANDS + SCHEDULED_REPORTS_INTERVAL_MIN) are used so existing
    deployments keep working; other users' entries never suppress those
    defaults. The schedule is re-read each loop, so /schedule add/remove/clear
    take effect without a redeploy.
    """
    if not config.SCHEDULED_REPORTS_ENABLED:
        log.info("SCHEDULED_REPORTS_ENABLED=false - scheduled reports off")
        return
    if not config.PROCESS_COMMANDS:
        log.info("PROCESS_COMMANDS=false - scheduled reports skipped (cron instance)")
        return
    default_chat = config.SCHEDULED_REPORTS_CHAT or config.TELEGRAM_CHAT_ID
    if not default_chat:
        log.warning("SCHEDULED_REPORTS_CHAT / TELEGRAM_CHAT_ID not set - scheduled reports off")
        return

    def _entries():
        return schedule_entries_with_defaults(default_chat)

    # (chat, commands) -> epoch seconds when the next run is due. Uses the
    # wall clock (time.time()) to match the persisted next_due timestamps that
    # storage.set_schedule_next_due writes to schedule.json - comparing
    # monotonic() against those epoch values would never fire.
    # Keyed on the entry's identity (not its list index) so one user adding
    # or removing their entries never changes another user's timing.
    next_due: dict = {}

    def _loop():
        while True:
            try:
                now = time.time()
                entries = _entries()
                if not entries:
                    log.info("scheduled reports: schedule is empty - nothing to run")
                    next_due.clear()
                    time.sleep(60)
                    continue
                # Drop due-times for entries that no longer exist so removed
                # schedules never linger, and (chat, commands) stays stable
                # across add/remove in other users' rows.
                alive = {
                    (str(entry.get("chat") or default_chat),
                     tuple(command for command in entry.get("commands") or [] if command.strip()))
                    for entry in entries
                }
                for key in [key for key in next_due if key not in alive]:
                    del next_due[key]
                for entry in entries:
                    interval = int(entry.get("interval_min") or config.SCHEDULED_REPORTS_INTERVAL_MIN)
                    commands = [command for command in entry.get("commands") or [] if command.strip()]
                    chat = str(entry.get("chat") or default_chat)
                    if not commands:
                        continue
                    key = (chat, tuple(commands))
                    # Persisted next-due (schedule.json) wins so the cadence
                    # survives redeploys. For a clock-time entry (run_at) the
                    # first due is the next occurrence of HH:MM in IST. Plain
                    # interval entries fall back to a short first-run delay.
                    persisted = storage.schedule_next_due_ts(entry)
                    due_ts = next_due.get(key)
                    if due_ts is None:
                        if persisted is not None:
                            due_ts = persisted
                        elif entry.get("run_at"):
                            due_ts = next_at_ist(entry["run_at"])
                            if due_ts is None:
                                due_ts = now + min(interval * 60, 60)
                        else:
                            due_ts = now + min(interval * 60, 60)
                        next_due[key] = due_ts
                    if now < due_ts:
                        continue
                    for command in commands:
                        try:
                            log.info("scheduled report: running %s (chat %s)", command, chat)
                            run_command(
                                chat, command,
                                schedule_source_label(entry, chat, command),
                            )
                        except Exception as error:  # one bad report must not stop the loop
                            log.warning(
                                "scheduled report %s failed: %s",
                                command, config.redact(error), exc_info=True,
                            )
                    # Schedule the next run AND persist it, so a redeploy
                    # resumes the same cadence instead of restarting the clock.
                    next_due_ts = time.time() + interval * 60
                    next_due[key] = next_due_ts
                    storage.set_schedule_next_due(chat, commands, interval, next_due_ts)
            except Exception as error:  # never let a scheduler hiccup kill the thread
                log.warning(
                    "scheduled reports loop error: %s",
                    config.redact(error), exc_info=True,
                )
            time.sleep(30)

    threading.Thread(target=_loop, daemon=True, name="scheduled-reports").start()
