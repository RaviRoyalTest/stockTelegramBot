"""Per-user alert & personalisation commands.

Each handler reads/writes the chat's settings.json entry through the storage
facade - one command family, one module.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

from .. import config, storage
from ..sources.types import ACTION_TYPES
from .reply import reply

log = logging.getLogger(__name__)


def watcher_universe(raw) -> str | None:
    """Normalize a watcher universe token (nifty100 / nifty500 / mylist).

    Returns None when the token is not a valid universe, so callers can
    reject bad input with a usage message.
    """
    universe = (raw or "").lower()
    if universe in ("nifty100", "n100", "100"):
        return "nifty100"
    if universe in ("nifty500", "n500", "500", "all"):
        return "nifty500"
    if universe in ("mylist", "watchlist", "list", "my"):
        return "mylist"
    return None


def handle_alertfilters(chat_id, parts) -> None:
    """Set the chat's action-type filters (/filter, /alertfilters).

    off/none now genuinely SILENCE corporate-action alerts (ca_alerts=False);
    all resets to every type; a type list keeps only those types.
    """
    settings = storage.get_user_settings(chat_id)
    current = settings.get("action_filters") or []
    ca_on = bool(settings.get("ca_alerts", True))
    if len(parts) < 2:
        state = "ON" if ca_on else "OFF"
        reply(
            chat_id,
            "Corporate-action alerts: <b>" + state + "</b>"
            + (" (types: <b>" + html.escape(", ".join(current) if current else "all") + "</b>)")
            + "\nUsage: <code>/filter dividend,bonus</code> \u00b7 <code>/filter all</code> \u00b7 "
            "<code>/filter off</code> (silence all corporate-action alerts)",
        )
        return
    raw = parts[1].lower()
    if raw in ("off", "none", "-", "false"):
        settings["ca_alerts"] = False
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F515 <b>Corporate-action alerts OFF.</b>\n"
            "No more automatic dividend / bonus / split / rights / buyback "
            "or ex-date reminders. Your saved type filters are kept - "
            "<code>/filter on</code> or <code>/filter all</code> turns them back on.\n"
            "On-demand queries like <code>/corpactions</code> still work anytime.",
        )
        return
    if raw in ("on", "enable", "true"):
        settings["ca_alerts"] = True
        settings["action_filters"] = settings.get("action_filters") or []
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F4E8 <b>Corporate-action alerts ON.</b>\n"
            + ("Types: <b>" + html.escape(", ".join(settings["action_filters"])) + "</b>"
               if settings["action_filters"]
               else "Receiving all action types again."),
        )
        return
    invalid = []
    if raw == "all":
        chosen = []
        settings["ca_alerts"] = True
    else:
        chosen = []
        for token in raw.split(","):
            token = token.strip()
            if token in ACTION_TYPES:
                chosen.append(token)
            elif token:
                invalid.append(token)
        settings["ca_alerts"] = True
    settings["action_filters"] = chosen
    storage.save_user_settings(chat_id, settings)
    log.info(
        "chat %s filters set to: %s",
        chat_id, ", ".join(chosen) if chosen else "all types",
    )
    message = "Filters set to: <b>" + html.escape(", ".join(chosen) if chosen else "all types") + "</b>"
    if invalid:
        message += f"\nIgnored unknown type(s): {html.escape(', '.join(invalid))}"
        message += f" (valid: {', '.join(ACTION_TYPES)})"
    reply(chat_id, message)


def handle_corpaction_alerts(chat_id, parts) -> None:
    """/corpactions on|off - turn corporate-action pushes on/off."""
    settings = storage.get_user_settings(chat_id)
    subcommand = parts[1].lower() if len(parts) > 1 else "status"
    if subcommand in ("off", "disable", "stop", "silence"):
        settings["ca_alerts"] = False
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F515 <b>Corporate-action alerts OFF.</b>\n"
            "You'll stop getting automatic dividend / bonus / split / rights / "
            "buyback and ex-date reminders.\n"
            "<code>/corpactions on</code> re-enables them anytime.\n"
            "On-demand queries still work: <code>/corpactions</code>, "
            "<code>/corpactionsformylist</code>.",
        )
        return
    if subcommand in ("on", "enable", "start"):
        settings["ca_alerts"] = True
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F4E8 <b>Corporate-action alerts ON.</b>\n"
            "Automatic dividend / bonus / split / rights / buyback and "
            "ex-date reminders are active again.",
        )
        return
    state = "ON" if settings.get("ca_alerts", True) else "OFF"
    reply(
        chat_id,
        "Corporate-action alerts: <b>" + state + "</b>\n"
        "<code>/corpactions on</code> \u00b7 <code>/corpactions off</code>",
    )


def handle_pricealert(chat_id, parts) -> None:
    """Set the chat's daily price-move threshold (/alert, /pricealert).

    off remembers the last threshold so /pricealert on restores it.
    """
    settings = storage.get_user_settings(chat_id)
    current = settings.get("price_alert_pct")
    last = settings.get("last_price_alert_pct")
    if len(parts) < 2:
        if current:
            reply(chat_id, f"Price alerts: <b>ON at {current:g}%</b>\nUsage: <code>/alert 3</code> (percent move) or <code>/alert off</code>")
        elif last:
            reply(chat_id, f"Price alerts: <b>OFF</b> (last was {last:g}%)\nUsage: <code>/alert 3</code> to set, <code>/alert on</code> to re-enable {last:g}%")
        else:
            reply(chat_id, "Price alerts are off.\nUsage: <code>/alert 3</code> (percent move) or <code>/alert off</code>")
        return
    raw = parts[1].lower()
    if raw in ("on", "enable", "start"):
        if last:
            settings["price_alert_pct"] = last
            storage.save_user_settings(chat_id, settings)
            reply(chat_id, f"Price alerts back <b>ON at {last:g}%</b>.")
        else:
            reply(chat_id, "No previous threshold to restore - set one first: <code>/alert 3</code>")
        return
    if raw in ("off", "none", "0", "0%"):
        value = None
    else:
        try:
            value = abs(float(raw.strip().rstrip("%")))
        except ValueError:
            reply(chat_id, "Usage: <code>/alert 3</code> (e.g. 3%), <code>/alert on</code> or <code>/alert off</code>")
            return
        if value == 0:
            value = None
    if value is not None:
        settings["last_price_alert_pct"] = value
    settings["price_alert_pct"] = value
    storage.save_user_settings(chat_id, settings)
    log.info(
        "chat %s price-alert threshold set to: %s",
        chat_id, "off" if value is None else f"{value:g}%",
    )
    reply(chat_id, f"Price alerts {'off' if value is None else 'set to <b>' + format(value, 'g') + '%</b>'}.")


def handle_watcher(chat_id, parts) -> None:
    """Manage the chat's sudden-move watcher (/watcher)."""
    settings = storage.get_user_settings(chat_id)
    watcher = settings.get("watcher") or {}
    subcommand = parts[1].lower() if len(parts) > 1 else "status"
    if subcommand in ("on", "enable", "start"):
        if not watcher.get("threshold"):
            watcher["threshold"] = 5.0
        watcher["enabled"] = True
        settings["watcher"] = watcher
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "\U0001F6A8 <b>Sudden-move watcher ON.</b>\n"
              f"I will alert you here when any stock in "
              f"<b>{watcher.get('universe', 'nifty100').upper()}</b> moves "
              f"\u2265 <b>{watcher.get('threshold', 5.0):g}%</b> in a session.\n"
              "Tune with <code>/watcher set 3</code> or <code>/watcher universe nifty500</code>.")
        return
    if subcommand in ("off", "disable", "stop"):
        watcher["enabled"] = False
        settings["watcher"] = watcher
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "\U0001F6A8 <b>Sudden-move watcher OFF.</b> No more big-move alerts.")
        return
    if subcommand in ("set", "threshold", "pct"):
        if len(parts) < 3:
            reply(chat_id, "Usage: <code>/watcher set 5</code> (percent move, e.g. 5 = 5%)")
            return
        try:
            value = abs(float(parts[2].strip().rstrip("%")))
        except ValueError:
            reply(chat_id, "Usage: <code>/watcher set 5</code> (percent move)")
            return
        if value == 0:
            value = 5.0
        watcher["threshold"] = value
        # Combined form: /watcher set 5 nifty500 sets threshold AND universe.
        universe = watcher_universe(parts[3]) if len(parts) >= 4 else None
        if universe is None and len(parts) >= 4:
            reply(chat_id, "Universe must be <code>nifty100</code>, <code>nifty500</code> or <code>mylist</code>.")
            return
        if universe:
            watcher["universe"] = universe
        watcher["enabled"] = bool(watcher.get("enabled"))
        settings["watcher"] = watcher
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, f"Watcher threshold set to <b>{value:g}%</b>"
              + (f" · universe <b>{universe.upper()}</b>" if universe else "") + " "
              + ("✅ ON" if watcher.get("enabled") else "(OFF - use <code>/watcher on</code> to enable)."))
        return
    if subcommand in ("universe", "scope", "market"):
        if len(parts) < 3:
            reply(chat_id, "Usage: <code>/watcher universe nifty100</code> | nifty500 | mylist")
            return
        universe = watcher_universe(parts[2])
        if universe is None:
            reply(chat_id, "Universe must be <code>nifty100</code>, <code>nifty500</code> or <code>mylist</code>.")
            return
        watcher["universe"] = universe
        # Combined form: /watcher universe nifty500 5 sets universe AND threshold.
        if len(parts) >= 4:
            try:
                threshold = abs(float(parts[3].strip().rstrip("%")))
                watcher["threshold"] = threshold if threshold > 0 else 5.0
            except ValueError:
                reply(chat_id, "Usage: <code>/watcher universe nifty500 5</code> (percent move)")
                return
        watcher["enabled"] = bool(watcher.get("enabled"))
        settings["watcher"] = watcher
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, f"Watcher universe set to <b>{universe.upper()}</b> "
              + ("ON" if watcher.get("enabled") else "OFF")
              + " - use <code>/watcher on</code> to enable.")
        return
    # status
    state = "ON" if watcher.get("enabled") else "OFF"
    reply(chat_id, "\U0001F6A8 <b>Sudden-move watcher</b>\n"
          f"Status: <b>{state}</b>\n"
          f"Threshold: <b>{watcher.get('threshold', 5.0):g}%</b> session move\n"
          f"Universe: <b>{(watcher.get('universe') or 'nifty100').upper()}</b>\n\n"
          "Usage:\n"
          "<code>/watcher on</code> / <code>/watcher off</code>\n"
          "<code>/watcher set 3</code>  (alert at 3% move)\n"
          "<code>/watcher universe nifty500</code>  (nifty100 | nifty500 | mylist)")


def handle_moversfund(chat_id, parts) -> None:
    """Set how movers reports show fundamentals (/fundmode, alias /moversfund)."""
    settings = storage.get_user_settings(chat_id)
    subcommand = parts[1].lower() if len(parts) > 1 else "status"
    if subcommand in ("button", "default", "reset", "on", "manual", "tap"):
        settings["movers_fund"] = "button"
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "\U0001F4CA <b>Movers fundamentals: button mode.</b>\n"
              "Every movers report now ends with a <b>Get Fundamentals</b> "
              "button - tap it to fetch P/E, ROCE/ROE, dividend yield etc. "
              "for that screen.")
        return
    if subcommand in ("auto", "full", "always", "complete"):
        settings["movers_fund"] = "auto"
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "\U0001F4CA <b>Movers fundamentals: auto mode.</b>\n"
              "Full fundamentals are now sent automatically with every "
              "movers report.")
        return
    mode = settings.get("movers_fund", "button")
    state = ("fundamentals sent automatically with every report"
             if mode == "auto"
             else "reports end with a <b>Get Fundamentals</b> button")
    reply(chat_id, f"\U0001F4CA <b>Movers fundamentals</b>\nMode: {state}\n\n"
          "Change it with <code>/fundmode button</code> (default) or "
          "<code>/fundmode auto</code>.")


def _parse_quiet_duration(raw) -> int | None:
    """Minutes for '90', '1h30m', '2h', '45m', '1d' (None when unparseable)."""
    if raw is None:
        return None
    if str(raw).isdigit():
        minutes = int(raw)
        return minutes if minutes > 0 else None
    match = re.fullmatch(r"(\d+d)?(\d+h)?(\d+m)?", str(raw).lower())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g[:-1]) if g else 0 for g in match.groups())
    total = days * 1440 + hours * 60 + minutes
    return total if total > 0 else None


def _quiet_duration_label(minutes: int) -> str:
    """'90' -> '1h 30m', '120' -> '2h', '45' -> '45 min'."""
    minutes = int(minutes)
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)} day(s)"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    if minutes > 60:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    return f"{minutes} min"


def _clock_ist(epoch: float) -> str:
    """HH:MM wall clock in IST for an epoch (host-local fallback)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(epoch, ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
    except Exception:
        return datetime.fromtimestamp(epoch).strftime("%H:%M")


def handle_quiet(chat_id, parts) -> None:
    """Pause ALL outgoing pushes temporarily (/quiet) - the master switch.

    on = pause until /quiet off; a duration (/quiet 2h, /quiet 30m, /quiet 90)
    = auto-resume; off = resume now. Command replies are never affected - only
    background pushes: corporate actions, ex-date reminders, price alerts,
    the watcher and scheduled reports.
    """
    settings = storage.get_user_settings(chat_id)
    subcommand = parts[1].lower() if len(parts) > 1 else "status"
    if subcommand in ("off", "resume", "stop", "end", "unpause"):
        settings["quiet"] = False
        settings.pop("quiet_until", None)
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F514 <b>Quiet mode OFF.</b>\n"
            "All alerts and scheduled reports are active again.",
        )
        return
    if subcommand in ("on", "start", "pause", "mute", "yes", "true"):
        settings["quiet"] = True
        settings["quiet_until"] = None
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F515 <b>Quiet mode ON.</b>\n"
            "All automatic messages are paused until you send "
            "<code>/quiet off</code>.\n"
            "Your commands still work - only background alerts pause.",
        )
        return
    minutes = _parse_quiet_duration(subcommand)
    if minutes is not None:
        now = time.time()
        until = now + minutes * 60
        settings["quiet"] = True
        settings["quiet_until"] = until
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "\U0001F515 <b>Quiet mode ON</b> for "
            + _quiet_duration_label(minutes) + ".\n"
            "All alerts resume automatically at <b>" + _clock_ist(until)
            + " IST</b>.\n"
            "Cancel early with <code>/quiet off</code>.",
        )
        return
    if storage.is_quiet(chat_id):
        until = storage.quiet_until_ts(chat_id)
        if until is None:
            reply(
                chat_id,
                "\U0001F515 <b>Quiet mode ON</b> - all alerts paused until "
                "you send <code>/quiet off</code>.",
            )
        else:
            mins = max(1, int((until - time.time()) / 60))
            reply(
                chat_id,
                "\U0001F515 <b>Quiet mode ON</b> - resumes in "
                + _quiet_duration_label(mins) + " (<b>" + _clock_ist(until)
                + " IST</b>).\n"
                "Cancel early: <code>/quiet off</code>.",
            )
        return
    reply(
        chat_id,
        "\U0001F514 <b>Quiet mode OFF</b> - all alerts active.\n\n"
        "Usage:\n"
        "<code>/quiet on</code>   \u2192 pause everything until <code>/quiet off</code>\n"
        "<code>/quiet 2h</code>   \u2192 pause for 2 hours (30m, 90, 1d also work)\n"
        "<code>/quiet off</code>  \u2192 resume now",
    )
