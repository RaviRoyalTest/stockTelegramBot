"""Settings + schedule report renderers for Telegram (/settings, /schedule)."""
from __future__ import annotations

import datetime as _dt
import html

from .. import config, storage


def fmt_next_run(due_ts: float) -> str:
    """Human-friendly 'next run' for a schedule entry, e.g. 'in 35 min (14:20 IST)'."""
    try:
        due = _dt.datetime.fromtimestamp(due_ts)
        mins = int((due_ts - _dt.datetime.now().timestamp()) / 60)
    except (TypeError, ValueError, OSError):
        return "soon"
    if mins <= 0:
        return "due now"
    if mins < 60:
        when = f"in {mins} min"
    elif mins < 24 * 60:
        when = f"in {mins // 60}h {mins % 60:02d}m"
    else:
        when = f"in {mins // (24 * 60)}d"
    return f"{when} ({due.strftime('%H:%M')})"


def format_settings(chat_id) -> str:
    """Render the per-chat customization settings (/settings)."""
    settings = storage.get_user_settings(chat_id)
    filters = settings.get("action_filters") or []
    alert = settings.get("price_alert_pct")
    watcher = settings.get("watcher") or {}
    owner = storage.is_owner(chat_id)
    where = (
        "watchlist.json (owner's list)"
        if owner
        else f"subscriptions.json (chat {chat_id})"
    )
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
            cmds = [c for c in config.SCHEDULED_COMMANDS if c.strip()]
            if not cmds:
                return "<b>Schedule:</b> no automated reports yet."
            return (
                "<b>Schedule (env defaults - use /schedule to edit)</b>\n"
                f"  1. every {config.SCHEDULED_REPORTS_INTERVAL_MIN} min: "
                + html.escape(", ".join(cmds))
                + "\n\n<b>Tip:</b> add your own entry below to replace these defaults."
            )
        return (
            "<b>Schedule:</b> no automated reports yet for your chat.\n"
            "Add one with <code>/schedule add 3h /scan500</code>."
        )
    lines = ["<b>Your schedule (schedule.json - pushed to GitHub)</b>"]
    for i, e in enumerate(mine, start=1):
        interval = int(e.get("interval_min") or 0)
        cmds = e.get("commands") or []
        label = f"every {interval} min"
        if interval and interval % (24 * 60) == 0:
            label = f"every {interval // (24 * 60)}d"
        elif interval and interval % 60 == 0:
            label = f"every {interval // 60}h"
        line = f"  {i}. {label}: {html.escape(', '.join(cmds))}"
        if e.get("run_at"):
            line += f" at {e['run_at']} IST"
        due = storage.schedule_next_due_ts(e)
        if due:
            line += f"  — next run {fmt_next_run(due)}"
        lines.append(line)
    lines.append(
        "\nUsage: <code>/schedule add 3h /scan500</code> (interval: 180, 90m, 3h, 1d)"
    )
    lines.append("<code>/schedule remove 1</code>  /  <code>/schedule clear</code>")
    lines.append("<code>/schedule run</code>  /  <code>/schednow</code> — run them all now")
    return "\n".join(lines)
