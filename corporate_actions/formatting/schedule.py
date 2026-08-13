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


def format_next_run(due_ts: float, tz_name: str | None = None, tz_tag: str = "") -> str:
    """Human-friendly 'next run' for a schedule entry, e.g. 'in 35 min (14:20 IST)'.

    The countdown is timezone-independent (epoch diff). The wall-clock shown
    is rendered in the entry's market timezone when `tz_name` is given (IST
    for India, America/New_York for the US) so the minute that fires matches
    the timezone the entry actually runs on, never the host's local clock.
    """
    try:
        minutes = int((due_ts - _datetime.datetime.now().timestamp()) / 60)
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                due_time = _datetime.datetime.fromtimestamp(due_ts, ZoneInfo(tz_name))
            except Exception:
                due_time = _datetime.datetime.fromtimestamp(due_ts)
        else:
            due_time = _datetime.datetime.fromtimestamp(due_ts)
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
    tag = f" {tz_tag}" if tz_tag else ""
    return f"{when} ({due_time.strftime('%H:%M')}{tag})"


def format_settings(chat_id) -> str:
    """Render the per-chat customization settings (/settings)."""
    settings = storage.get_user_settings(chat_id)
    filters = settings.get("action_filters") or []
    alert = settings.get("price_alert_pct")
    watcher = settings.get("watcher") or {}
    owner = storage.is_owner(chat_id)
    where = storage.list_location(chat_id)
    ca_state = "off" if settings.get("ca_alerts", True) is False else (
        "on" + (f" ({', '.join(filters)}) " if filters else " (all types)")
    )
    quiet = storage.is_quiet(chat_id)
    quiet_state = "paused - all alerts muted" if quiet else "active"
    return "\n".join(
        [
            "<b>Your settings</b>",
            f"Chat id: {chat_id}",
            f"Role: {'owner' if owner else 'subscriber'}",
            "Corporate-action alerts: " + ca_state,
            "Price alert: " + ("off" if not alert else f"{float(alert):g}%"),
            "Watcher: " + ("off" if not watcher.get("enabled")
                           else f"on at {float(watcher.get('threshold') or 5):g}% "
                                f"({(watcher.get('universe') or 'nifty100').upper()})"),
            "Movers fundamentals: " + ("auto" if settings.get("movers_fund") == "auto" else "button"),
            "Quiet mode: " + quiet_state,
            f"Your list is saved in: {where}",
            "Customize with /corpactions on|off, /pricealert, /watcher, /fundmode and /quiet.",
        ]
    )


def format_schedule(chat_id) -> str:
    """Render the requester's OWN automated-report schedule (/schedule).

    Every user only ever sees and manages their own entries - another
    person's reports never appear here and are never affected by this
    chat's /schedule add/remove/clear. Each row shows the cadence, the
    market-hours gate / run window and any active pause.
    """
    from ..market.hours import (
        entry_market,
        entry_paused,
        entry_paused_until,
        market_label,
        market_tz_name,
        market_tz_tag,
    )

    default_market = (storage.get_user_settings(chat_id) or {}).get(
        "schedule_market", config.SCHEDULED_REPORTS_MARKET
    )
    mine = storage.load_schedule_for(chat_id)
    if not mine:
        if storage.is_owner(chat_id):
            commands = [command for command in config.SCHEDULED_COMMANDS if command.strip()]
            if not commands:
                return "<b>Schedule:</b> no automated reports yet."
            return (
                "<b>Schedule (env defaults - use /schedule to edit)</b>\n"
                f"Gate: <b>{market_label(default_market)}</b>\n"
                f"  1. every {config.SCHEDULED_REPORTS_INTERVAL_MIN} min: "
                + html.escape(", ".join(commands))
                + "\n\n<b>Tip:</b> add your own entry below to replace these defaults."
            )
        return (
            "<b>Schedule:</b> no automated reports yet for your chat.\n"
            "Add one with <code>/schedule add 3h /scan500</code>."
        )
    lines = [
        "<b>Your schedule (schedule.json - pushed to GitHub)</b>",
        f"Gate: <b>{market_label(default_market)}</b> (change with /market)",
    ]
    for index, entry in enumerate(mine, start=1):
        interval = int(entry.get("interval_min") or 0)
        commands = entry.get("commands") or []
        market = entry_market(entry, default=default_market)
        tz_tag = market_tz_tag(market)
        tz_name = market_tz_name(market)
        # A clock-time entry on a 24h cadence reads as 'daily at HH:MM', not
        # the confusing 'every 1d: /cmd at 09:15 IST'.
        if entry.get("run_at") and interval and interval % (24 * 60) == 0:
            label = "daily"
        else:
            label = format_interval(interval)
        line = f"  {index}. {label}: {html.escape(', '.join(commands))}"
        if entry.get("run_at"):
            pretty_times = str(entry["run_at"]).replace(",", ", ")
            line += f" at {pretty_times} {tz_tag}"
        if entry.get("window_start") and entry.get("window_end"):
            line += f" \u00b7 window {entry['window_start']}\u2013{entry['window_end']} {tz_tag}"
        elif market != "any":
            line += f" \u00b7 {market_label(market)}"
        elif entry.get("market") == "any":
            line += " \u00b7 any time"
        if entry_paused(entry):
            line += f" \u2014 \u23f8 <b>paused</b> (until {entry_paused_until(entry)})"
        due_time = storage.schedule_next_due_ts(entry)
        if due_time:
            line += f"  \u2014 next run {format_next_run(due_time, tz_name, tz_tag)}"
        lines.append(line)
    lines.append(
        "\nUsage: <code>/schedule add 3h /scan500</code> (interval: 180, 90m, 3h, 1d)"
    )
    lines.append("Market gate: append <code>us</code>, <code>any</code> or "
                 "<code>in from HH:MM to HH:MM</code> to /schedule add")
    lines.append("Open + close results: <code>/schedule add at 09:15,15:30 /cmd</code> - "
                 "daily at both times; a run window fires at its start AND end.")
    lines.append("<code>/schedule pause 1d|3d|1w|2w|1mo</code>  /  "
                 "<code>/schedule resume</code>")
    lines.append("<code>/schedule remove 1</code>  /  <code>/schedule clear</code>")
    lines.append("<code>/schedule run</code>  /  <code>/schednow</code> — run them all now")
    return "\n".join(lines)
