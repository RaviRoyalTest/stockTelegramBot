"""Per-user alert & personalisation commands.

Each handler reads/writes the chat's settings.json entry through the storage
facade - one command family, one module.
"""
from __future__ import annotations

import html
import logging

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
    """Set the chat's action-type filters (/filter, /alertfilters)."""
    settings = storage.get_user_settings(chat_id)
    current = settings.get("action_filters") or []
    if len(parts) < 2:
        reply(
            chat_id,
            "Current filters: <b>" + html.escape(", ".join(current) if current else "all types") + "</b>"
            + "\nUsage: <code>/filter dividend,bonus</code> or <code>/filter all</code>",
        )
        return
    raw = parts[1].lower()
    invalid = []
    if raw in ("all", "off", "none", "-"):
        chosen = []
    else:
        chosen = []
        for token in raw.split(","):
            token = token.strip()
            if token in ACTION_TYPES:
                chosen.append(token)
            elif token:
                invalid.append(token)
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


def handle_pricealert(chat_id, parts) -> None:
    """Set the chat's daily price-move threshold (/alert, /pricealert)."""
    settings = storage.get_user_settings(chat_id)
    current = settings.get("price_alert_pct")
    if len(parts) < 2:
        if current:
            reply(chat_id, f"Current price-alert threshold: <b>{current:g}%</b>")
        else:
            reply(chat_id, "Price alerts are off.\nUsage: <code>/alert 3</code> (percent move) or <code>/alert off</code>")
        return
    raw = parts[1].lower()
    if raw in ("off", "none", "0", "0%"):
        value = None
    else:
        try:
            value = abs(float(raw.strip().rstrip("%")))
        except ValueError:
            reply(chat_id, "Usage: <code>/alert 3</code> (e.g. 3%) or <code>/alert off</code>")
            return
        if value == 0:
            value = None
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
    """Set how movers reports show fundamentals (/moversfund)."""
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
          "Change it with <code>/moversfund button</code> (default) or "
          "<code>/moversfund auto</code>.")
