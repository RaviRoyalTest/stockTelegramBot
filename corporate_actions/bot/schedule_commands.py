"""Menu + scheduled-reports commands: /menu, /schedule, /schednow, /market."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime

from .. import config, storage
from ..formatting.schedule import format_schedule
from ..market.hours import is_market_open, market_label, market_tz_tag
from ..telegram.markup import hide_keyboard_markup, quick_menu_markup
from .helpers import run_command_sequence
from .help_texts import QUICK_MENU_TEXT
from .reply import reply
from .schedule_parsing import (
    MARKET_WORDS,
    parse_interval_min,
    parse_pause_minutes,
    parse_schedule_options,
    valid_hhmm,
)

log = logging.getLogger(__name__)


def handle_menu(chat_id, parts) -> None:
    """Show (or hide) the one-tap reply-keyboard menu (/menu, /quick)."""
    subcommand = parts[1].lower() if len(parts) > 1 else ""
    if subcommand in ("off", "hide", "none", "remove"):
        reply(
            chat_id,
            "Quick menu hidden. Send <code>/menu</code> anytime to bring it back.",
            reply_markup=hide_keyboard_markup(),
        )
        return
    reply(chat_id, QUICK_MENU_TEXT, reply_markup=quick_menu_markup())


def _run_at_tz_tag(market) -> str:
    """'IST' for the Indian market, 'ET' for the US market."""
    return market_tz_tag(market)


def run_schedule_now(chat_id) -> None:
    """Run every command in the requester's OWN schedule immediately (/schednow).

    Loads the chat's schedule entries (including the owner's env defaults when
    the owner has no file entries) and fires each command now, in order. This
    is purely a manual trigger - it does not change the auto-schedule timing.
    The intro names the commands AND the watchlist the results relate to.
    """
    mine = storage.load_schedule_for(chat_id)
    commands = []
    for entry in mine:
        commands.extend(command for command in (entry.get("commands") or []) if command.strip())
    if not commands:
        if storage.is_owner(chat_id):
            commands = [command for command in config.SCHEDULED_COMMANDS if command.strip()]
        if not commands:
            reply(chat_id, "No scheduled commands for your chat yet.\n"
                  "Add one with <code>/schedule add 3h /scan500</code>, then "
                  "use <code>/schedule run</code> to fire it now.")
            return
    run_command_sequence(
        chat_id,
        commands,
        intro="\u23f0 <b>Running your schedule now</b>\n"
              "Reports are on their way - the bigger scans take a minute or two.",
        done="\u2705 <b>Schedule run complete.</b>",
        source_note=storage.list_location(chat_id),
    )


def market_status_text(chat_id) -> str:
    """Live market-hours status + the chat's default gate (/market, /schedule market)."""
    default = (storage.get_user_settings(chat_id) or {}).get(
        "schedule_market", config.SCHEDULED_REPORTS_MARKET
    )
    now = time.time()
    return "\n".join([
        "<b>\U0001F30D Scheduled-report market-hours gate</b>",
        f"  India: <b>{'OPEN' if is_market_open('in', now) else 'CLOSED'}</b>  \u00b7  {market_label('in')}",
        f"  US:    <b>{'OPEN' if is_market_open('us', now) else 'CLOSED'}</b>  \u00b7  {market_label('us')}",
        f"Your default gate: <b>{market_label(default)}</b>",
        "",
        "Set the default with <code>/market in | us | any</code> or per entry "
        "with <code>/schedule add 3h /cmd us</code>.",
    ])


def handle_market(chat_id, parts) -> None:
    """Show or set the market-hours gate for YOUR scheduled reports (/market).

    /market in         -> run scheduled reports only during Indian market hours
    /market us         -> run scheduled reports only during US market hours
    /market any | off  -> no gate - run any time the timer fires
    /market            -> live market status + your current default gate
    """
    subcommand = parts[1].lower() if len(parts) > 1 else ""
    if subcommand in MARKET_WORDS:
        value = "any" if subcommand == "off" else subcommand
        settings = storage.get_user_settings(chat_id) or {}
        settings["schedule_market"] = value
        storage.save_user_settings(chat_id, settings)
        log.info("chat %s set schedule market gate to %s", chat_id, value)
        reply(
            chat_id,
            f"Market-hours gate set to <b>{market_label(value)}</b>.\n"
            "New /schedule entries will use it. You can still override per entry "
            "with <code>/schedule add 3h /cmd us</code>.",
        )
        return
    if subcommand:
        reply(
            chat_id,
            "Usage: <code>/market in | us | any | off</code>\n"
            "  <code>/market in</code> \u2192 only Indian market hours (NSE/BSE 09:15\u201315:30 IST)\n"
            "  <code>/market us</code> \u2192 only US market hours (NASDAQ/NYSE 09:30\u201316:00 ET)\n"
            "  <code>/market any</code> \u2192 no gate - run any time\n"
            "  <code>/market</code> \u2192 live status + your current gate",
        )
        return
    reply(chat_id, market_status_text(chat_id))


def handle_sched(chat_id, parts) -> None:
    """Manage YOUR OWN automated-report schedule (works for every user).

    /schedule                  -> show YOUR schedule
    /schedule add <int> <cmd...> -> add a command on its own timer (e.g. /schedule add 3h /scan500)
    /schedule remove <n>       -> remove YOUR entry n (1-based, as shown by /schedule)
    /schedule clear            -> remove all of YOUR entries
    /schedule pause <dur>      -> pause YOUR whole schedule (1d, 2d, 3d, 1w, 2w, 1mo...)
    /schedule resume           -> resume YOUR schedule early
    /schedule market in|us|any -> set YOUR default market-hours gate

    Everything is scoped to the requesting chat - one user's schedule can
    never change or disturb another user's.
    """
    cmd_name = parts[0].lower()
    subcommand = parts[1].lower() if len(parts) > 1 else ""
    if cmd_name == "/schednow" and not subcommand:
        run_schedule_now(chat_id)
        return
    if subcommand == "add":
        handle_schedule_add(chat_id, parts)
        return
    if subcommand == "remove":
        if len(parts) < 3:
            reply(chat_id, "Usage: <code>/schedule remove &lt;n&gt;</code> (number shown by /schedule).")
            return
        try:
            index = int(parts[2]) - 1
        except ValueError:
            reply(chat_id, "Usage: <code>/schedule remove &lt;n&gt;</code>")
            return
        entries = storage.load_schedule_for(chat_id)
        if index < 0 or index >= len(entries):
            reply(chat_id, "No entry at that number. Run /schedule to list them.")
            return
        storage.remove_schedule_entry(chat_id, index)
        log.info("chat %s removed schedule entry %d", chat_id, index)
        reply(chat_id, f"Removed entry {index + 1}.\n\n{format_schedule(chat_id)}")
        return

    if subcommand == "clear":
        storage.clear_schedule(chat_id)
        log.info("chat %s cleared their schedule", chat_id)
        reply(
            chat_id,
            "Your schedule cleared - no automated reports will run "
            "for your chat. Other users' schedules are untouched.",
        )
        return

    if subcommand == "pause":
        if len(parts) < 3:
            reply(
                chat_id,
                "Usage: <code>/schedule pause &lt;duration&gt;</code>\n"
                "e.g. <code>/schedule pause 1d</code>, <code>2d</code>, <code>3d</code>, "
                "<code>1w</code>, <code>2w</code> or <code>1mo</code>. "
                "Resume with <code>/schedule resume</code>.",
            )
            return
        minutes = parse_pause_minutes(parts[2])
        if minutes is None:
            reply(
                chat_id,
                "Bad pause duration. Use e.g. <code>12h</code>, <code>1d</code>, "
                "<code>3d</code>, <code>1w</code>, <code>2w</code> or <code>1mo</code>.",
            )
            return
        until_ts = time.time() + minutes * 60
        storage.pause_schedule(chat_id, until_ts)
        until_label = datetime.fromtimestamp(until_ts).strftime("%d-%b %H:%M")
        log.info("chat %s paused schedule for %s (%s)", chat_id, parts[2], until_label)
        reply(
            chat_id,
            f"\u23f8 <b>Schedule paused</b> for <b>{parts[2].lower()}</b> - "
            f"no automatic reports until <b>{until_label}</b>.\n"
            "Use <code>/schedule resume</code> to start it earlier.",
        )
        return

    if subcommand in ("resume", "unpause"):
        storage.resume_schedule(chat_id)
        log.info("chat %s resumed their schedule", chat_id)
        reply(
            chat_id,
            "\u25b6\ufe0f <b>Schedule resumed</b> - automatic reports will "
            "run again on their normal timers.",
        )
        return

    if subcommand == "market":
        handle_market(chat_id, ["/market"] + parts[2:])
        return

    if subcommand in ("run", "now", "force"):
        run_schedule_now(chat_id)
        return

    reply(chat_id, format_schedule(chat_id))


def handle_schedule_add(chat_id, parts) -> None:
    """/schedule add - add a command on its own timer with optional gating.

    Grammar (options come AFTER the command):
      /schedule add <interval> <command...> [in|us|any] [from HH:MM to HH:MM]
      /schedule add at HH:MM [interval] <command...> [in|us|any] [from HH:MM to HH:MM]

    market in -> only during Indian market hours, us -> US market hours,
    any/off -> no gating. An explicit window overrides the market hours.
    """
    if len(parts) < 4:
        reply(
            chat_id,
            "Usage: <code>/schedule add &lt;interval&gt; &lt;command&gt;</code>\n"
            "e.g. <code>/schedule add 3h /scan500</code> or "
            "<code>/schedule add 90m /topmovers 30m</code>\n"
            "Or at a clock time: <code>/schedule add at 09:15 /toplosers 1h</code> "
            "(daily at 09:15 IST) or <code>/schedule add at 09:15 3h /cmd</code> "
            "(every 3h from 09:15).\n"
            "Options after the command: <code>in|us|any</code> (market-hours gate) "
            "and <code>from HH:MM to HH:MM</code> (run window).\n"
            "Interval: minutes (180), m (90m), h (3h) or d (1d), min 15.",
        )
        return
    run_at = None
    token2 = parts[2].lower()
    interval_tok = parts[2]
    cmd_start = 3
    if token2 in ("at", "time", "@"):
        # /schedule add at HH:MM [interval] <command>
        if len(parts) < 5:
            reply(
                chat_id,
                "Usage: <code>/schedule add at HH:MM &lt;command&gt;</code>\n"
                "e.g. <code>/schedule add at 09:15 /toplosers 1h</code> (daily at 09:15 IST) "
                "or <code>/schedule add at 09:15 3h /cmd</code> (every 3h).",
            )
            return
        run_at = parts[3].strip()
        if not valid_hhmm(run_at):
            reply(chat_id, "Bad time. Use 24h format like <code>09:15</code> or <code>18:30</code>.")
            return
        next_due_ts = parts[4] if len(parts) > 4 else ""
        if next_due_ts.startswith("/"):
            # No interval given -> daily at run_at
            interval_tok = "1440m"
            cmd_start = 4
        else:
            interval_tok = next_due_ts
            cmd_start = 5
    interval = parse_interval_min(interval_tok)
    if interval is None:
        reply(
            chat_id,
            "Bad interval. Use e.g. <code>180</code>, <code>90m</code>, "
            "<code>3h</code> or <code>1d</code> (min 15 minutes).",
        )
        return
    command_tokens, options = parse_schedule_options(list(parts[cmd_start:]))
    command = " ".join(command_tokens).strip()
    if not command.startswith("/"):
        reply(chat_id, "The command must start with / (e.g. <code>/scan500</code>).")
        return
    if command.lower().split()[0] in ("/sched", "/schedule"):
        reply(chat_id, "You cannot schedule /schedule itself.")
        return
    market = options.get("market") or (storage.get_user_settings(chat_id) or {}).get(
        "schedule_market", config.SCHEDULED_REPORTS_MARKET
    )
    if market not in ("in", "us", "any"):
        market = "in"
    storage.add_schedule_entry(
        interval, [command], str(chat_id), run_at=run_at,
        market=market,
        window_start=options.get("window_start"),
        window_end=options.get("window_end"),
    )
    log.info(
        "chat %s added schedule entry: every %d min%s market=%s -> %s",
        chat_id, interval, f" at {run_at}" if run_at else "", market, command,
    )
    when_bits = []
    if run_at:
        tz_tag = _run_at_tz_tag(market)
        if interval % 1440 == 0:
            when_bits.append(f"daily at <b>{run_at} {tz_tag}</b>")
        else:
            when_bits.append(f"every <b>{interval} min</b> starting at <b>{run_at} {tz_tag}</b>")
    else:
        when_bits.append(f"every <b>{interval} min</b>")
    if market != "any":
        when_bits.append(f"only during <b>{market_label(market)}</b>")
    else:
        when_bits.append("any time")
    if options.get("window_start"):
        tz_tag = _run_at_tz_tag(market)
        when_bits.append(
            f"window <b>{options['window_start']}\u2013{options['window_end']} {tz_tag}</b>"
        )
    reply(
        chat_id,
        f"Added: <code>{html.escape(command)}</code> " + " \u00b7 ".join(when_bits) + ".\n\n"
        f"{format_schedule(chat_id)}",
    )
