"""Settings + schedule report renderers for Telegram (/settings, /schedule)."""
from __future__ import annotations

import datetime as _datetime
import html

from .. import config, storage


def format_interval(interval_min: int) -> str:
    """Human label for a minute interval: 'every 180 min' / 'every 3h' / 'every 1d'."""
    interval = int(interval_min or 0)
    if interval and interval % (24 * 60) == 0:
        return f"every {interval // (24 * 60)}d"
    if interval and interval % 60 == 0:
        return f"every {interval // 60}h"
    return f"every {interval} min"


def format_next_run(due_ts: float) -> str:
    """Human-friendly 'next run' for a schedule entry, e.g. 'in 35 min (14:20 IST)'."""
    try:
        due_time = _datetime.datetime.fromtimestamp(due_ts)
        minutes = int((due_ts - _datetime.datetime.now().timestamp()) / 60)
    except (TypeError, ValueError, OSError):
        return "soon"
    if minutes <= 0:
        return "due now"
    if minutes < 60:
        when = f"in {minutes} min"
    elif minutes < 24 * 60:
        when = f"in {minutes // 60}h {minutes % 60:02d}m"
    else:
        when = f"in {minutes // (24 * 60)}d"
    return f"{when} ({due_time.strftime('%H:%M')})"


def format_settings(chat_id) -> str:
    """Render the per-chat customization settings (/settings)."""
    settings = storage.get_user_settings(chat_id)
    filters = settings.get("action_filters") or []
    alert = settings.get("price_alert_pct")
    watcher = settings.get("watcher") or {}
    owner = storage.is_owner(chat_id)
    where = storage.list_location(chat_id)
    return "\n".join(
        [
            "<b>Your settings</b>",
            f"Chat id: {chat_id}",
            f"Role: {'owner' if owner else 'subscriber'}",
            "Action filters: " + (", ".join(filters) if filters else "all types"),
            "Price alert: " + ("off" if not alert else f"{float(alert):g}%"),
            "Watcher: " + ("off" if not watcher.get("enabled")
                           else f"on at {float(watcher.get('threshold') or 5):g}% "
                                f"({(watcher.get('universe') or 'nifty100').upper()})"),
            "Movers fundamentals: " + ("auto" if settings.get("movers_fund") == "auto" else "button"),
            f"Your list is saved in: {where}",
            "Customize with /alertfilters, /pricealert, /watcher and /moversfund.",
        ]
    )


def format_schedule(chat_id) -> str:
    """Render the requester's OWN automated-report schedule (/schedule).

    Every user only ever sees and manages their own entries - another
    person's reports never appear here and are never affected by this
    chat's /schedule add/remove/clear.
    """
    mine = storage.load_schedule_for(chat_id)
    if not mine:
        if storage.is_owner(chat_id):
            commands = [command for command in config.SCHEDULED_COMMANDS if command.strip()]
            if not commands:
                return "<b>Schedule:</b> no automated reports yet."
            return (
                "<b>Schedule (env defaults - use /schedule to edit)</b>\n"
                f"  1. every {config.SCHEDULED_REPORTS_INTERVAL_MIN} min: "
                + html.escape(", ".join(commands))
                + "\n\n<b>Tip:</b> add your own entry below to replace these defaults."
            )
        return (
            "<b>Schedule:</b> no automated reports yet for your chat.\n"
            "Add one with <code>/schedule add 3h /scan500</code>."
        )
    lines = ["<b>Your schedule (schedule.json - pushed to GitHub)</b>"]
    for index, entry in enumerate(mine, start=1):
        interval = int(entry.get("interval_min") or 0)
        commands = entry.get("commands") or []
        label = format_interval(interval)
        line = f"  {index}. {label}: {html.escape(', '.join(commands))}"
        if entry.get("run_at"):
            line += f" at {entry['run_at']} IST"
        due_time = storage.schedule_next_due_ts(entry)
        if due_time:
            line += f"  — next run {format_next_run(due_time)}"
        lines.append(line)
    lines.append(
        "\nUsage: <code>/schedule add 3h /scan500</code> (interval: 180, 90m, 3h, 1d)"
    )
    lines.append("<code>/schedule remove 1</code>  /  <code>/schedule clear</code>")
    lines.append("<code>/schedule run</code>  /  <code>/schednow</code> — run them all now")
    return "\n".join(lines)
