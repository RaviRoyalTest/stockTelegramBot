"""Menu + scheduled-reports commands: /menu, /schedule, /schednow."""
from __future__ import annotations

import html
import logging
import re

from .. import config, storage
from ..core.dates import next_at_ist
from ..formatting.schedule import format_schedule
from ..telegram.markup import hide_keyboard_markup, quick_menu_markup
from .help_texts import QUICK_MENU_TEXT
from .reply import reply

log = logging.getLogger(__name__)


def handle_menu(chat_id, parts) -> None:
    """Show (or hide) the one-tap reply-keyboard menu (/menu, /quick)."""
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("off", "hide", "none", "remove"):
        reply(
            chat_id,
            "Quick menu hidden. Send <code>/menu</code> anytime to bring it back.",
            reply_markup=hide_keyboard_markup(),
        )
        return
    reply(chat_id, QUICK_MENU_TEXT, reply_markup=quick_menu_markup())


def parse_interval_min(raw: str) -> int | None:
    """Parse an interval like '180', '3h', '90m', '1d' into minutes.

    Returns None when the value is unparseable or below the 15-minute floor.
    """
    m = re.fullmatch(r"(\d+)\s*([mhd])?", str(raw or "").strip().lower())
    if not m:
        return None
    minutes = int(m.group(1))
    unit = m.group(2) or "m"
    if unit == "h":
        minutes *= 60
    elif unit == "d":
        minutes *= 24 * 60
    if minutes < 15:
        return None
    return minutes


def run_schedule_now(chat_id) -> None:
    """Run every command in the requester's OWN schedule immediately (/schednow).

    Loads the chat's schedule entries (including the owner's env defaults when
    the owner has no file entries) and fires each command now, in order. This
    is purely a manual trigger - it does not change the auto-schedule timing.
    """
    mine = storage.load_schedule_for(chat_id)
    cmds = []
    for e in mine:
        cmds.extend(c for c in (e.get("commands") or []) if c.strip())
    if not cmds:
        if storage.is_owner(chat_id):
            cmds = [c for c in config.SCHEDULED_COMMANDS if c.strip()]
        if not cmds:
            reply(chat_id, "No scheduled commands for your chat yet.\n"
                  "Add one with <code>/schedule add 3h /scan500</code>, then "
                  "use <code>/schedule run</code> to fire it now.")
            return
    reply(
        chat_id,
        "\u23f0 <b>Running your schedule now</b>\n"
        + "\n".join(f"  \u2022 <code>{html.escape(c)}</code>" for c in cmds)
        + "\n\nReports are on their way - the bigger scans take a minute or two.",
    )
    for cmd in cmds:
        try:
            log.info("schednow: running %s (chat %s)", cmd, chat_id)
            from .dispatch import handle_command  # late import: breaks the module cycle
            handle_command(chat_id, cmd)
        except Exception as exc:
            log.warning("schednow: command %s failed: %s", cmd, config.redact(exc), exc_info=True)
            try:
                reply(chat_id, f"<code>{html.escape(cmd)}</code> failed: {html.escape(config.redact(str(exc)))}")
            except Exception:
                pass
    reply(chat_id, "\u2705 <b>Schedule run complete.</b>")


def handle_sched(chat_id, parts) -> None:
    """Manage YOUR OWN automated-report schedule (works for every user).

    /schedule                  -> show YOUR schedule
    /schedule add <int> <cmd...> -> add a command on its own timer (e.g. /schedule add 3h /scan500)
    /schedule remove <n>       -> remove YOUR entry n (1-based, as shown by /schedule)
    /schedule clear            -> remove all of YOUR entries

    Everything is scoped to the requesting chat - one user's schedule can
    never change or disturb another user's.
    """
    cmd_name = parts[0].lower()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if cmd_name == "/schednow" and not sub:
        run_schedule_now(chat_id)
        return
    if sub == "add":
        if len(parts) < 4:
            reply(
                chat_id,
                "Usage: <code>/schedule add &lt;interval&gt; &lt;command&gt;</code>\n"
                "e.g. <code>/schedule add 3h /scan500</code> or "
                "<code>/schedule add 90m /topmovers 30m</code>\n"
                "Or at a clock time: <code>/schedule add at 09:15 /toplosers 1h</code> "
                "(daily at 09:15 IST) or <code>/schedule add at 09:15 3h /cmd</code> "
                "(every 3h from 09:15).\n"
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
            if next_at_ist(run_at) is None:
                reply(chat_id, "Bad time. Use 24h format like <code>09:15</code> or <code>18:30</code> (IST).")
                return
            nxt = parts[4] if len(parts) > 4 else ""
            if nxt.startswith("/"):
                # No interval given -> daily at run_at
                interval_tok = "1440m"
                cmd_start = 4
            else:
                interval_tok = nxt
                cmd_start = 5
        interval = parse_interval_min(interval_tok)
        if interval is None:
            reply(
                chat_id,
                "Bad interval. Use e.g. <code>180</code>, <code>90m</code>, "
                "<code>3h</code> or <code>1d</code> (min 15 minutes).",
            )
            return
        command = " ".join(parts[cmd_start:]).strip()
        if not command.startswith("/"):
            reply(chat_id, "The command must start with / (e.g. <code>/scan500</code>).")
            return
        if command.lower().split()[0] in ("/sched", "/schedule"):
            reply(chat_id, "You cannot schedule /schedule itself.")
            return
        storage.add_schedule_entry(interval, [command], str(chat_id), run_at=run_at)
        log.info(
            "chat %s added schedule entry: every %d min%s -> %s",
            chat_id, interval, f" at {run_at}" if run_at else "", command,
        )
        when = f"every <b>{interval} min</b> starting at <b>{run_at} IST</b>" if run_at else f"every <b>{interval} min</b>"
        reply(
            chat_id,
            f"Added: <code>{html.escape(command)}</code> {when}.\n\n{format_schedule(chat_id)}",
        )
        return

    if sub == "remove":
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

    if sub == "clear":
        storage.clear_schedule(chat_id)
        log.info("chat %s cleared their schedule", chat_id)
        reply(
            chat_id,
            "Your schedule cleared - no automated reports will run "
            "for your chat. Other users' schedules are untouched.",
        )
        return

    if sub in ("run", "now", "force"):
        run_schedule_now(chat_id)
        return

    reply(chat_id, format_schedule(chat_id))
