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
from .core.dates import next_at_in_tz_after
from .formatting.schedule import format_interval
from .market.hours import (
    entry_in_window,
    entry_market,
    entry_paused,
    market_label,
    market_open_close,
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
        pretty_times = str(entry["run_at"]).replace(",", ", ")
        when += f" at {pretty_times} {_tz_tag(entry)}"
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


def _run_at_times(entry: dict) -> list[str]:
    """Clock times (HH:MM) an entry fires at daily.

    run_at may hold a comma-separated list, e.g. '09:15,15:30' - the open +
    close reports - each of which becomes a daily anchor.
    """
    raw = (entry or {}).get("run_at") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _add_minutes_hhmm(hhmm: str, minutes: int) -> str:
    """'09:15' + 60 -> '10:15' (wraps at midnight)."""
    hour, minute = (int(part) for part in str(hhmm).split(":"))
    total = (hour * 60 + minute + int(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _window_anchor_times(entry: dict, interval_min: int) -> list[str]:
    """Daily anchor clock-times for an entry with an explicit run window.

    window_start, then interval ticks, ending EXACTLY at window_end - so a
    windowed entry always fires at the start of the session AND at the end,
    whatever the interval. Overnight windows (end <= start) anchor at the
    two edges only. Empty when the entry has no explicit window.
    """
    start = str((entry or {}).get("window_start") or "").strip()
    end = str((entry or {}).get("window_end") or "").strip()
    if not start or not end:
        return []
    interval = max(15, int(interval_min or 60))

    def _minutes(hhmm):
        hour, minute = (int(part) for part in hhmm.split(":"))
        return hour * 60 + minute

    if _minutes(end) <= _minutes(start):  # overnight window - edges only
        return [start, end]
    times = [start]
    tick = start
    while True:
        next_tick = _add_minutes_hhmm(tick, interval)
        # next_tick == tick guards against intervals >= 24h wrapping back to
        # the same clock time; _minutes >= end stops the grid at the window
        # edge. Either way the loop always advances or breaks.
        if next_tick == tick or _minutes(next_tick) >= _minutes(end):
            break
        times.append(next_tick)
        tick = next_tick
    if times[-1] != end:
        times.append(end)
    return times


def _next_clock_after(times: list[str], tz_name: str, after_ts: float) -> float | None:
    """Epoch of the next occurrence of ANY of `times` (daily) after `after_ts`."""
    candidates = [
        next_at_in_tz_after(hhmm, tz_name, after_ts)
        for hhmm in times if hhmm
    ]
    candidates = [ts for ts in candidates if ts is not None]
    return min(candidates) if candidates else None


def _hhmm_minutes(hhmm: str) -> int:
    """'15:30' -> 930 (minutes since midnight)."""
    hour, minute = (int(part) for part in str(hhmm).split(":"))
    return hour * 60 + minute


def _market_open_close(entry: dict) -> tuple | None:
    """(open, close) "HH:MM" of the entry's market gate, or None for 'any'."""
    market = entry_market(entry, default=config.SCHEDULED_REPORTS_MARKET)
    if market == "any":
        return None
    return market_open_close(market)


def _entry_anchor_times(entry: dict, interval_min: int) -> list[str]:
    """Daily clock-time anchors governing an entry's firing sequence.

    Explicit run window -> grid from window_start by interval, ending exactly
    at window_end. Explicit clock times (run_at, possibly a comma list) fire
    EXACTLY at the listed times - the user chose them, so no close is added
    (a daily 09:15 /gappers report stays a 09:15 report). Interval entries
    ("every N") gated to a market get a grid from market open by interval
    ending at close - which keeps the cadence EXACT (09:15, 10:15, ...) and
    fires once more at the session close (15:30 IST / 16:00 ET) so the
    closing data is never lost. market=any keeps pure interval behaviour
    with no close anchor.
    """
    windowed = _window_anchor_times(entry, interval_min)
    if windowed:
        return windowed
    times = _run_at_times(entry)
    oc = _market_open_close(entry)
    close = oc[1] if oc else None
    if times:
        if len(times) > 1:
            # Explicit multi-time list: fire at the listed times only.
            return times
        run_at = times[0]
        if close and interval_min % (24 * 60) != 0 \
                and _hhmm_minutes(run_at) <= _hhmm_minutes(close):
            # 'at 09:15 3h' style: every <interval> from run_at, ending at
            # the session close.
            return _window_anchor_times(
                {"window_start": run_at, "window_end": close}, interval_min,
            )
        # Daily at HH:MM -> exactly that time (no auto close run).
        return times
    if close:
        # Plain interval entry gated to a market: grid from open to close.
        return _window_anchor_times(
            {"window_start": oc[0], "window_end": oc[1]}, interval_min,
        )
    return []


def _first_due(entry: dict, market: str, interval_min: int, after_ts: float) -> float:
    """First-run due for an entry without a persisted next_due.

    Anchored entries (a run window or clock times) start at their next anchor
    in the market's wall clock (IST for India, ET for the US); plain interval
    entries fall back to a short first-run delay so the first report lands
    soon after the server boots.
    """
    anchors = _entry_anchor_times(entry, interval_min)
    if anchors:
        ts = _next_clock_after(anchors, market_tz_name(market), after_ts)
        if ts is not None:
            return ts
    return after_ts + min(interval_min * 60, 60)


# How long after an anchor an anchored entry may still fire even though the
# half-open window check has closed (e.g. the closing report at 15:30, or a
# late wake a minute or two after the bell).
_GATE_GRACE_SECONDS = 600  # 10 minutes


def _gate_allows(entry: dict, market: str, now: float, due_ts: float | None,
                 interval_min: int) -> bool:
    """Whether an entry may fire at `now` for a due at `due_ts`.

    The regular window/market-hours gate, plus one exception: an anchored
    entry (run window or clock times) may fire up to GATE_GRACE_SECONDS after
    an anchor even when the window check has already closed - otherwise the
    end-of-session report (window_end, e.g. 15:30) would be swallowed by the
    half-open "now < close" check.
    """
    if entry_in_window(entry, default=market, now=now):
        return True
    if due_ts is None or not _entry_anchor_times(entry, interval_min):
        return False
    return 0 <= now - due_ts <= _GATE_GRACE_SECONDS


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
                    # survives redeploys. Otherwise anchored entries (a run
                    # window or clock times) start at their next anchor in the
                    # market's wall clock (IST for India, ET for the US); plain
                    # interval entries get a short first-run delay.
                    persisted = storage.schedule_next_due_ts(entry)
                    due_ts = next_due.get(key)
                    if due_ts is None:
                        if persisted is not None:
                            due_ts = persisted
                        else:
                            due_ts = _first_due(entry, market, interval, now)
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
                    # explicit window, or briefly after an anchor - see
                    # _gate_allows). When the timer lands off-hours we wait,
                    # so the report arrives at the next session instead of being
                    # skipped entirely.
                    if not _gate_allows(entry, market, now, due_ts, interval):
                        log.info(
                            "scheduled report: %s outside run window (market=%s) - waiting for next session",
                            key, market,
                        )
                        # Never loop on a stale due forever: once the due has
                        # been blocked well past its time, jump to the next
                        # anchor (e.g. a server that woke long after the close
                        # skips yesterday's close report and waits for the next
                        # session's start instead of retrying it forever).
                        if now - due_ts > _GATE_GRACE_SECONDS:
                            next_anchor = _first_due(entry, market, interval, now)
                            if next_anchor > now:
                                next_due[key] = next_anchor
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
                    # Anchored entries continue along their daily clock-time
                    # sequence (window start/end, run_at list); a single clock
                    # time with a sub-daily interval keeps the interval cadence
                    # (e.g. 'at 09:15 3h' -> 09:15, 12:15, 15:15...).
                    after_ts = time.time()
                    anchors = _entry_anchor_times(entry, interval)
                    # Grid / multi-time / daily anchors chain along their clock
                    # sequence (exactly 09:15, 10:15, ... ending at the session
                    # close); a single clock time with a sub-daily interval
                    # keeps the interval cadence (e.g. 'at 20:00 3h' -> 20:00,
                    # 23:00...).
                    if len(anchors) > 1 or (anchors and interval % (24 * 60) == 0):
                        next_due_ts = _next_clock_after(
                            anchors, market_tz_name(market), after_ts,
                        )
                        if next_due_ts is None:
                            next_due_ts = after_ts + interval * 60
                    else:
                        next_due_ts = after_ts + interval * 60
                    next_due[key] = next_due_ts
                    storage.set_schedule_next_due(chat, commands, interval, next_due_ts)
            except Exception as error:  # never let a scheduler hiccup kill the thread
                log.warning(
                    "scheduled reports loop error: %s",
                    config.redact(error), exc_info=True,
                )
            time.sleep(30)

    threading.Thread(target=_loop, daemon=True, name="scheduled-reports").start()
