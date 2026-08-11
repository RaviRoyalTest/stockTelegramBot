"""Command registry: aliases, usage/status hints and the Telegram menu.

The dispatcher (dispatch.py) reads the tables here - extending the bot means
adding an entry to these tables + a handler module, never editing dispatch.
"""
from __future__ import annotations

import logging

from .. import storage
from ..core.text import split_messages
from ..formatting.schedule import format_schedule, format_settings
from ..telegram.client import is_configured, set_my_commands
from ..telegram.markup import quick_menu_markup
from . import ca_cmds, scan_cmds, settings_cmds, status as status_cmds, watchlist_cmds
from .help_texts import CA_HELP
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def _watcher_status_text(chat_id) -> str:
    watcher = (storage.get_user_settings(chat_id) or {}).get("watcher") or {}
    state = "ON" if watcher.get("enabled") else "OFF"
    return (
        f"\U0001F6A8 <b>Sudden-move watcher</b>\n"
        f"Status: <b>{state}</b>\n"
        f"Threshold: <b>{watcher.get('threshold', 5.0):g}%</b> session move\n"
        f"Universe: <b>{(watcher.get('universe') or 'nifty100').upper()}</b>"
    )


def _pricealert_status_text(chat_id) -> str:
    alert = (storage.get_user_settings(chat_id) or {}).get("price_alert_pct")
    return "Price alerts: <b>" + ("off" if not alert else f"{float(alert):g}%") + "</b>"


def _moversfund_status_text(chat_id) -> str:
    mode = (storage.get_user_settings(chat_id) or {}).get("movers_fund", "button")
    if mode == "auto":
        state = "fundamentals sent automatically with every report"
    else:
        state = "price report ends with a <b>Get Fundamentals</b> button"
    return f"\U0001F4CA <b>Movers fundamentals</b>\nMode: {state}"


def _alertfilters_status_text(chat_id) -> str:
    filters = (storage.get_user_settings(chat_id) or {}).get("action_filters") or []
    return "Action filters: <b>" + (", ".join(filters) if filters else "all types") + "</b>"


def _schedule_status_text(chat_id) -> str:
    return format_schedule(chat_id)


COMMAND_STATUS = {
    "/watcher": _watcher_status_text,
    "/pricealert": _pricealert_status_text,
    "/alertfilters": _alertfilters_status_text,
    "/schedule": _schedule_status_text,
    "/moversfund": _moversfund_status_text,
}


# Old short aliases -> their canonical command. Used so a BARE alias (e.g.
# /next, /summary, /stock, /fund) shows the same self-explaining hint as its
# main command, while argument-ful aliases keep working exactly as before.
ALIAS_TO_MAIN = {
    "/list": "/watchlist",
    "/next": "/corpactionsformylist",
    "/upcoming": "/corpactionsformylist",
    "/summary": "/corpactionssummary",
    "/casummary": "/corpactionssummary",
    "/ca": "/corpactions",
    "/corporate-actions": "/corpactions",
    "/corp-actions": "/corpactions",
    "/actions": "/corpactions",
    "/exdate": "/exdates",
    "/ex-dates": "/exdates",
    "/sched": "/schedule",
    "/alert": "/pricealert",
    "/filter": "/alertfilters",
    "/actionfilters": "/alertfilters",
    "/stock": "/fundamentalanalyze",
    "/info": "/fundamentalanalyze",
    "/quote": "/fundamentalanalyze",
    "/analysis": "/fundamentalanalyze",
    "/stockanalysis": "/fundamentalanalyze",
    "/stock-analysis": "/fundamentalanalyze",
    "/fundamental-analysis": "/fundamentalanalyze",
    "/fund": "/fundamentalreport",
    "/fundamentals": "/fundamentalreport",
    "/harmonic": "/harmonicpatterns",
    "/movers": "/topmovers",
    "/marketmovers": "/topmovers",
    "/gainers": "/topgainers",
    "/losers": "/toplosers",
    "/favorites": "/myfavourites",
    "/favourites": "/myfavourites",
    "/mypicks": "/myfavourites",
    "/dailybrief": "/myfavourites",
    "/quick": "/menu",
    "/shortcuts": "/menu",
    "/buttons": "/menu",
    "/bigmover": "/watcher",
    "/moverwatch": "/watcher",
}


COMMAND_USAGE = {
    "/corpactions": CA_HELP,
    "/exdates": (
        "<b>/exdates</b> - corporate actions by ex-date window\n"
        "/exdates today   \u2192 ex-dates due today\n"
        "/exdates 7       \u2192 ex-dates within the next 7 days\n"
        "/exdates         \u2192 default window (5 days)"
    ),
    "/news": (
        "<b>/news</b> - latest headlines for your watchlist stocks\n"
        "/news            \u2192 news for all watchlist stocks\n"
        "/news 5          \u2192 5 headlines per stock\n"
        "/news RELIANCE   \u2192 news for RELIANCE only"
    ),
    "/fundamentalanalyze": (
        "<b>/fundamentalanalyze</b> - quick analysis card\n"
        "/fundamentalanalyze TATATECH  \u2192 one stock\n"
        "/fundamentalanalyze mylist    \u2192 your whole watchlist\n"
        "/fundamentalanalyze 5-10      \u2192 watchlist stocks #5-#10"
    ),
    "/fundamentalreport": (
        "<b>/fundamentalreport</b> - DEEP fundamental report\n"
        "/fundamentalreport RELIANCE  \u2192 one stock\n"
        "/fundamentalreport mylist    \u2192 your whole watchlist\n"
        "/fundamentalreport 3-5       \u2192 watchlist stocks #3-#5"
    ),
    "/harmonicpatterns": (
        "<b>/harmonicpatterns</b> - harmonic pattern scanner\n"
        "/harmonicpatterns all       \u2192 NIFTY 100, daily\n"
        "/harmonicpatterns 500 1w    \u2192 NIFTY 500, weekly\n"
        "/harmonicpatterns RELIANCE  \u2192 full report (PRZ, entry, SL)\n"
        "Timeframes: 5m 15m 30m 1h 4h 1d 1w  (alias /harmonic)"
    ),
    "/topmovers": (
        "<b>/topmovers</b> - top gainers AND losers\n"
        "/topmovers          \u2192 last 1h, NIFTY 100\n"
        "/topmovers 2d 500   \u2192 2-day movers, NIFTY 500\n"
        "/topmovers 1w 10    \u2192 top 10 movers this week"
    ),
    "/topgainers": (
        "<b>/topgainers</b> - top rising stocks\n"
        "/topgainers 1h         \u2192 last 1h gainers\n"
        "/topgainers 1mo 20 500 \u2192 top 20 gainers this month, NIFTY 500\n"
        "/topgainers 100        \u2192 top 100 gainers"
    ),
    "/toplosers": (
        "<b>/toplosers</b> - top falling stocks\n"
        "/toplosers 1h 10       \u2192 top 10 losers last hour\n"
        "/toplosers 1w nifty100 \u2192 weekly losers, NIFTY 100\n"
        "/toplosers 100         \u2192 top 100 losers"
    ),
    "/schedule": (
        "<b>/schedule</b> - your automated reports (per user)\n"
        "/schedule add 3h /scan500          \u2192 every 3 hours\n"
        "/schedule add at 09:15 /toplosers 1h  \u2192 daily at 09:15 IST\n"
        "/schedule run | /schednow          \u2192 run them all right now\n"
        "/schedule remove 1                 \u2192 remove YOUR entry #1\n"
        "/schedule clear                    \u2192 remove all of yours"
    ),
    "/pricealert": (
        "<b>/pricealert</b> - daily price-move alerts\n"
        "/pricealert 3   \u2192 alert when a stock moves \u00b13% in a day\n"
        "/pricealert off \u2192 disable price alerts\n"
        "/pricealert     \u2192 show current threshold"
    ),
    "/alertfilters": (
        "<b>/alertfilters</b> - receive only the action types you choose\n"
        "/alertfilters dividend,bonus \u2192 only those types\n"
        "/alertfilters all            \u2192 reset to all types"
    ),
    "/watcher": (
        "Usage:\n"
        "/watcher on    \u2192 turn it ON\n"
        "/watcher off   \u2192 turn it OFF\n"
        "/watcher set 3 \u2192 alert at a 3% session move (e.g. /watcher set 5 nifty500)\n"
        "/watcher universe nifty500 \u2192 nifty100 | nifty500 | mylist\n"
        "/watcher       \u2192 show current status"
    ),
    "/moversfund": (
        "<b>/moversfund</b> - how movers reports show fundamentals\n"
        "/moversfund button \u2192 price report ends with a <b>Get Fundamentals</b> button (default)\n"
        "/moversfund auto   \u2192 send full fundamentals automatically with every report\n"
        "/moversfund        \u2192 show current mode"
    ),
    "/addstock": (
        "<b>/addstock</b> - add a stock to your watchlist\n"
        "/addstock RELIANCE NSE  \u2192 add RELIANCE (NSE)\n"
        "/addstock PGINVIT       \u2192 add with default exchange NSE"
    ),
    "/removestock": (
        "<b>/removestock</b> - remove a stock from your watchlist\n"
        "/removestock TCS        \u2192 remove TCS"
    ),
}


# Commands that already do something useful when typed bare: describe them
# first, then still run them, so the user sees BOTH what it does and the
# result. value = (description, runnable(chat_id)).
DESCRIBE_AND_RUN = {
    "/watchlist": (
        "\U0001F4CB <b>/watchlist</b> - shows YOUR current watchlist with prices. "
        "Add stocks with /addstock, remove with /removestock.",
        lambda cid: watchlist_cmds.send_watchlist(cid),
    ),
    "/settings": (
        "\U00002699\ufe0f <b>/settings</b> - shows YOUR filters, price-alert and watcher "
        "settings. Change them with /alertfilters, /pricealert and /watcher.",
        lambda cid: reply(cid, format_settings(cid)),
    ),
    "/status": (
        "\U0001F4CA <b>/status</b> - where your watchlist is saved, GitHub push status "
        "and your personal data scope.",
        lambda cid: status_cmds.handle_status(cid),
    ),
    "/corpactionssummary": (
        "\U0001F4CA <b>/corpactionssummary</b> - corporate-action snapshot: counts "
        "by exchange &amp; type plus the next ex-dates. Details: /corpactions.",
        lambda cid: ca_cmds.run_ca_query(cid, {"mode": "overview"}),
    ),
    "/corpactionsformylist": (
        "\U0001F4C5 <b>/corpactionsformylist</b> - corporate actions for YOUR "
        "watchlist: upcoming ex-dates plus recently passed / in-progress actions "
        "with status (payment due, rights window, etc.).",
        lambda cid: watchlist_cmds.send_watchlist_actions(cid),
    ),
    "/scan500": (
        "\U0001F50D <b>/scan500</b> - full NIFTY 500 technical scanner (EMAs, RSI, "
        "MACD, ADX, Supertrend, VWAP...). Takes ~1-2 min - starting it now.",
        lambda cid: scan_cmds.handle_scan500(cid, ["/scan500"]),
    ),
    "/checknow": (
        "\u26A1 <b>/checknow</b> - force-runs an alert check now and re-sends "
        "every matching alert (corporate actions, reminders, price moves).",
        lambda cid: reply(
            cid,
            "Running a forced check now - re-sending all matching alerts shortly.",
        ),
    ),
}


def _bare_command_usage(chat_id, cmd) -> bool:
    """When a main command is typed with no arguments, explain it.

    Commands with subcommands get the subcommand list (COMMAND_USAGE);
    commands that already produce useful output get a short description AND
    are still run (DESCRIBE_AND_RUN), so nothing useful is lost.
    Returns True when a hint was sent.
    """
    usage = COMMAND_USAGE.get(cmd)
    if usage:
        # CURRENT status first (e.g. watcher ON/OFF, current filters, your
        # schedule), then the subcommand list - so nothing is lost.
        status_fn = COMMAND_STATUS.get(cmd)
        status = status_fn(chat_id) if status_fn else ""
        reply(chat_id, (status + "\n\n" if status else "") + usage)
        return True
    described = DESCRIBE_AND_RUN.get(cmd)
    if described:
        reply(chat_id, described[0])
        described[1](chat_id)
        return True
    return False


def send_help(chat_id):
    """Send the styled HTML help message (/help, /start, unknown commands)."""
    from .help_texts import HELP_TEXT

    reply_messages(
        chat_id,
        split_messages(HELP_TEXT.split("\n")),
        reply_markup=quick_menu_markup(),
    )


def register_commands() -> bool:
    """Publish the bot's command menu via Telegram setMyCommands."""
    if not is_configured():
        return False
    menu = [
        {"command": "corpactionsformylist", "description": "Corporate actions for YOUR watchlist with status"},
        {"command": "myfavourites", "description": "Run your favourite commands in one go"},
        {"command": "corpactions", "description": "Browse all NSE+BSE corporate actions"},
        {"command": "exdates", "description": "All actions by ex-date: today or next N days"},
        {"command": "corpactionssummary", "description": "Corporate-action snapshot: counts + next ex-dates"},
        {"command": "watchlist", "description": "Show your watchlist"},
        {"command": "addstock", "description": "Add a stock: /addstock RELIANCE NSE"},
        {"command": "removestock", "description": "Remove a stock from your watchlist"},
        {"command": "news", "description": "Latest news for your watchlist stocks"},
        {"command": "fundamentalanalyze", "description": "Analysis card or watchlist range: /fundamentalanalyze mylist"},
        {"command": "fundamentalreport", "description": "Deep report or range: /fundamentalreport mylist"},
        {"command": "harmonicpatterns", "description": "Harmonic pattern scan NIFTY 100/500: /harmonicpatterns all"},
        {"command": "scan500", "description": "NIFTY 500 CNC/MIS technical scanner"},
        {"command": "topmovers", "description": "Top gainers AND losers with fundamentals"},
        {"command": "topgainers", "description": "Top rising stocks with fundamentals"},
        {"command": "toplosers", "description": "Top falling stocks with fundamentals"},
        {"command": "alertfilters", "description": "Receive only chosen action types"},
        {"command": "pricealert", "description": "Alert on +/-PCT% daily price move"},
        {"command": "settings", "description": "Show your current settings"},
        {"command": "schedule", "description": "Auto reports: /schedule add 3h /scan500"},
        {"command": "status", "description": "Check persistence / GitHub push"},
        {"command": "checknow", "description": "Force a check and resend alerts"},
        {"command": "watcher", "description": "Big-move alerts: /watcher on, off, set 5, universe nifty500"},
        {"command": "moversfund", "description": "Movers: button vs auto fundamentals"},
        {"command": "all", "description": "Show every command - copy & send any line"},
        {"command": "menu", "description": "One-tap command buttons - no typing"},
        {"command": "schednow", "description": "Run all your scheduled commands right now"},
        {"command": "help", "description": "Show all commands and examples"},
    ]
    return set_my_commands(menu)
