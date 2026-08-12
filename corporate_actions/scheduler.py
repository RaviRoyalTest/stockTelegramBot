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
import threading
import time

from . import config, storage
from .core.dates import next_at_in_tz, next_at_ist
from .formatting.schedule import format_interval
from .market.hours import (
    entry_in_window,
    entry_market,
    entry_paused,
    market_label,
    market_tz_name,
    market_tz_tag,
)

log = logging.getLogger(__name__)


def _run_at_timezone(entry: dict) -> str:
    """Wall-clock timezone of an entry's run_at: its market's, IST by default."""
    market = entry_market(entry, default=config.SCHEDULED_REPORTS_MARKET)
    return market_tz_name(market)


def schedule_source_label(entry: dict, chat, command: str) -> str:
    """HTML banner naming the schedule entry that produced this report.

    Sent before the scheduled command runs so the user always knows which
    scheduled task (and which watchlist) the result belongs to: the entry's
    cadence, its market-hours gate / run window, the exact command and the
    chat's watchlist location.
    """
    when = format_interval(entry.get("interval_min") or config.SCHEDULED_REPORTS_INTERVAL_MIN)
    if entry.get("run_at"):
        when += f" at {entry['run_at']} {_tz_tag(entry)}"
    gate_bits = []
    market = entry_market(entry, default=config.SCHEDULED_REPORTS_MARKET)
    if entry.get("window_start") and entry.get("window_end"):
        # An explicit run window overrides the market-hours gate.
        gate_bits.append(
            f"window {entry['window_start']}\u2013{entry['window_end']} ({_tz_tag(entry)})"
        )
    elif market != "any":
        gate_bits.append(f"only during {market_label(market)} market hours")
    if gate_bits:
        when += " \u00b7 " + " \u00b7 ".join(gate_bits)
    origin = "env default" if entry.get("default") else "your /schedule entry"
    where = storage.list_location(chat)
    return (
        f"\U0001F4C5 <b>Scheduled report</b> \u00b7 {origin} \u00b7 {when}\n"
        f"Command: <code>{html.escape(command)}</code>\n"
        f"Source list: <code>{html.escape(where)}</code> \u2014 the results "
        "below come from this scheduled task."
    )


def _tz_tag(entry: dict) -> str:
    market = entry_market(entry, default=config.SCHEDULED_REPORTS_MARKET)
    return market_tz_tag(market)


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
                "market": config.SCHEDULED_REPORTS_MARKET,
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
                    market = entry_market(entry, default=config.SCHEDULED_REPORTS_MARKET)
                    # Persisted next-due (schedule.json) wins so the cadence
                    # survives redeploys. For a clock-time entry (run_at) the
                    # first due is the next occurrence of HH:MM in the market's
                    # wall clock (IST for India, ET for the US). Plain
                    # interval entries fall back to a short first-run delay.
                    persisted = storage.schedule_next_due_ts(entry)
                    due_ts = next_due.get(key)
                    if due_ts is None:
                        if persisted is not None:
                            due_ts = persisted
                        elif entry.get("run_at"):
                            due_ts = next_at_in_tz(entry["run_at"], market_tz_name(market))
                            if due_ts is None:
                                due_ts = now + min(interval * 60, 60)
                        else:
                            due_ts = now + min(interval * 60, 60)
                        next_due[key] = due_ts
                    if now < due_ts:
                        continue
                    # Paused entries wait until the pause lapses (auto-resume),
                    # without advancing the timer so the report still fires once
                    # the pause ends.
                    if entry_paused(entry, now):
                        log.info(
                            "scheduled report: %s paused until %s - skipping",
                            key, (entry or {}).get("paused_until"),
                        )
                        continue
                    # Market-hours / run-window gate: an automatic report only
                    # fires while the entry's market is open (or inside its
                    # explicit window). When the timer lands off-hours we wait,
                    # so the report arrives at the next session instead of being
                    # skipped entirely.
                    if not entry_in_window(entry, default=market, now=now):
                        log.info(
                            "scheduled report: %s outside run window (market=%s) - waiting for next session",
                            key, market,
                        )
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
