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
from ..telegram.markup import inline_command_buttons, quick_menu_markup, recent_buttons
from . import corporate_action_commands, scanner_commands, schedule_commands, settings_commands, status as status_commands, watchlist_commands
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


def _market_status_text(chat_id) -> str:
    return schedule_commands.market_status_text(chat_id)


COMMAND_STATUS = {
    "/watcher": _watcher_status_text,
    "/pricealert": _pricealert_status_text,
    "/alertfilters": _alertfilters_status_text,
    "/schedule": _schedule_status_text,
    "/moversfund": _moversfund_status_text,
    "/market": _market_status_text,
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
    "/usfund": "/usstock",
    "/usquote": "/usstock",
    "/us": "/usstock",
    "/investcheck": "/checklist",
    "/scorecard": "/checklist",
    "/qualitycheck": "/checklist",
    "/quality": "/checklist",
    "/harmonic": "/harmonicpatterns",
    "/ind": "/indicator",
    "/tech": "/indicator",
    "/technical": "/indicator",
    "/analyst": "/forecast",
    "/forecastanalysis": "/forecast",
    "/guide": "/learn",
    "/explain": "/learn",
    "/tutorial": "/learn",
    "/howto": "/learn",
    "/movers": "/topmovers",
    "/marketmovers": "/topmovers",
    "/gap": "/gappers",
    "/gaps": "/gappers",
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
    "/moverlist": "/bigmovers",
    "/watcherlist": "/bigmovers",
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
    "/usstock": (
        "<b>/usstock</b> - US stock details (price + deep fundamentals in USD)\n"
        "/usstock AAPL  \u2192 Apple fundamentals (P/E, D/E, margins, analyst targets...)\n"
        "/usstock MSFT  \u00b7  /usstock NVDA  \u00b7  /usstock BRK-B\n"
        "Schedule it: <code>/schedule add 3h /usstock AAPL us</code> (US market hours only)\n"
        "Aliases: /usfund, /usquote, /us"
    ),
    "/harmonicpatterns": (
        "<b>/harmonicpatterns</b> - harmonic pattern scanner\n"
        "/harmonicpatterns all       \u2192 NIFTY 100, daily\n"
        "/harmonicpatterns 500 1w    \u2192 NIFTY 500, weekly\n"
        "/harmonicpatterns RELIANCE  \u2192 full report (PRZ, entry, SL)\n"
        "Timeframes: 5m 15m 30m 1h 4h 1d 1w  (alias /harmonic)"
    ),
    "/gappers": (
        "<b>/gappers</b> - overnight gap scanner: prev close \u2192 open gaps\n"
        "/gappers              \u2192 top 15 gap-DOWNs today (default), NIFTY 500\n"
        "/gappers up | all     \u2192 gap-UPs only / both directions\n"
        "/gappers 1d | 2d | 3d \u2192 the gaps that opened 1 / 2 / 3 sessions ago\n"
        "/gappers window 3d    \u2192 today's OPEN vs the close 3 sessions ago\n"
        "/gappers 12-08-2026   \u2192 the gaps that opened ON that date\n"
        "                       (12 Aug \u00b7 12aug \u00b7 2026-08-12 \u00b7 Aug 12 all work)\n"
        "/gappers 08-08-2026 12-08-2026  \u2192 cumulative gap over a period:\n"
        "                       open on the end date vs the close before the start\n"
        "/gappers 20 nifty100  \u2192 top 20, NIFTY 100 \u00b7 /gappers sp500 \u2192 S&P 500\n"
        "/gappers GODREJCP     \u2192 that stock's recent gap history (close \u2192 next open)"
    ),
    "/bigmovers": (
        "<b>/bigmovers</b> - ALL stocks beyond a % session move, one full list\n"
        "Same threshold idea as /watcher, but a single report like /toplosers\n"
        "instead of one alert per stock.\n\n"
        "<b>Quick picks</b>\n"
        "<code>/bigmovers</code>           \u2192 every stock up/down \u2265 5% today, NIFTY 500\n"
        "<code>/bigmovers 8</code>         \u2192 \u2265 8% session move\n"
        "<code>/bigmovers 3 nifty100</code> \u2192 NIFTY 100\n"
        "<code>/bigmovers 8 sp500</code>   \u2192 S&P 500\n"
        "<code>/bigmovers 5 nasdaq100</code> \u2192 NASDAQ 100\n\n"
        "Move = current price vs previous close (both directions, ranked by |move|)."
    ),
    "/topmovers": (
        "<b>/topmovers</b> - top gainers AND losers (default: last 1h, NIFTY 100)\n"
        "Tap a command below to copy it, then send it.\n\n"
        "<b>Quick picks</b>\n"
        "<code>/topmovers</code>             \u2192 last 1h, NIFTY 100\n"
        "<code>/topmovers 1h 10</code>       \u2192 top 10 movers last hour\n"
        "<code>/topmovers today 25</code>    \u2192 today\u2019s top 25 movers\n"
        "<code>/topmovers 1w 500</code>      \u2192 weekly movers, NIFTY 500\n"
        "<code>/topmovers 1mo nifty100</code> \u2192 monthly movers, NIFTY 100\n"
        "<code>/topmovers today nasdaq100</code> \u2192 today\u2019s NASDAQ 100 movers\n"
        "<code>/topmovers 12-08-2026</code>  \u2192 that day\u2019s historical movers (any date)\n\n"
        "<b>Periods</b>  5m 15m 30m 1h 2h 4h today 1d 2d 5d 1w 2w 1mo 3mo 6mo 1y  or a date (12-08-2026, 12 Aug)\n"
        "<b>Universe</b>  n100/nifty100 \u00b7 n500/nifty500 \u00b7 nasdaq100/ndx/us "
        "(bare 100/500 picks the index here)\n"
        "Each row shows P/E, sector P/E, 52W range, div yield, holdings &amp; D/E "
        "(via the Get Fundamentals button - change with /moversfund)."
    ),
    "/topgainers": (
        "<b>/topgainers</b> - top rising stocks (default: today, NIFTY 500, top 30)\n"
        "Tap a command below to copy it, then send it.\n\n"
        "<b>Quick picks</b>\n"
        "<code>/topgainers</code>          \u2192 today\u2019s top 30 gainers\n"
        "<code>/topgainers 1h</code>       \u2192 last 1h gainers\n"
        "<code>/topgainers 1h 10</code>    \u2192 top 10 gainers last hour\n"
        "<code>/topgainers 100</code>      \u2192 top 100 gainers today\n"
        "<code>/topgainers 1mo 20 500</code> \u2192 top 20 monthly gainers, NIFTY 500\n"
        "<code>/topgainers today nasdaq100</code> \u2192 today\u2019s NASDAQ 100 gainers\n"
        "<code>/topgainers 12-08-2026</code>  \u2192 that day\u2019s historical gainers (any date)\n\n"
        "<b>Periods</b>  5m 15m 30m 1h 2h 4h today 1d 2d 5d 1w 2w 1mo 3mo 6mo 1y  or a date (12-08-2026, 12 Aug)\n"
        "<b>Universe</b>  n100/nifty100 \u00b7 n500/nifty500 \u00b7 nasdaq100/ndx/us\n"
        "Note: for gainers a bare 100/500 is the top-N count (e.g. /topgainers 100 = "
        "top 100) - use nifty100/nifty500 to pick the index. "
        "Each row also shows P/E, sector P/E, 52W range, div yield, holdings &amp; D/E."
    ),
    "/toplosers": (
        "<b>/toplosers</b> - top falling stocks (default: today, NIFTY 500, top 30)\n"
        "Tap a command below to copy it, then send it.\n\n"
        "<b>Quick picks</b>\n"
        "<code>/toplosers</code>              \u2192 today\u2019s top 30 losers\n"
        "<code>/toplosers 1h</code>           \u2192 losers in the last hour\n"
        "<code>/toplosers 1h 10</code>        \u2192 top 10 losers last hour\n"
        "<code>/toplosers 100</code>          \u2192 top 100 losers today\n"
        "<code>/toplosers 1w nifty100</code>  \u2192 weekly losers, NIFTY 100\n"
        "<code>/toplosers today nasdaq100</code> \u2192 today\u2019s NASDAQ 100 losers\n"
        "<code>/toplosers 12-08-2026</code>  \u2192 that day\u2019s historical losers (any date)\n\n"
        "<b>More periods</b>\n"
        "<code>/toplosers 30m</code>          \u2192 last 30 minutes\n"
        "<code>/toplosers 2d</code>           \u2192 last 2 days\n"
        "<code>/toplosers 1w</code>           \u2192 last week\n"
        "<code>/toplosers 1mo</code>          \u2192 last month\n\n"
        "<b>Universe + count</b>\n"
        "<code>/toplosers nifty500</code>     \u2192 whole NIFTY 500\n"
        "<code>/toplosers 20 500</code>       \u2192 top 20 losers, NIFTY 500\n"
        "<code>/toplosers 1h 5 nifty100</code> \u2192 top 5 losers last hour, NIFTY 100\n\n"
        "<b>Periods</b>  5m 15m 30m 1h 2h 4h today 1d 2d 5d 1w 2w 1mo 3mo 6mo 1y  or a date (12-08-2026, 12 Aug)\n"
        "<b>Universe</b>  n100/nifty100 \u00b7 n500/nifty500 \u00b7 nasdaq100/ndx/us\n"
        "Note: for losers a bare 100/500 is the top-N count (e.g. /toplosers 100 = "
        "top 100) - use nifty100/nifty500 to pick the index. "
        "Each row shows P/E, sector P/E, 52W range, div yield, holdings &amp; D/E "
        "(via the Get Fundamentals button - change with /moversfund)."
    ),
    "/checklist": (
        "<b>/checklist</b> - 32-point investment scorecard "
        "(10 personal + 22 AI criteria)\n"
        "/checklist RELIANCE  \u2192 scorecard for one stock\n"
        "/checklist mylist    \u2192 your whole watchlist (3 per batch)\n"
        "/checklist 5-10      \u2192 watchlist positions #5-#10\n"
        "Aliases: /investcheck, /scorecard, /qualitycheck"
    ),
    "/indicator": (
        "<b>/indicator</b> - clear deep-dive for ONE indicator\n"
        "/indicator RELIANCE RSI  \u2192 value, signal, trend &amp; how to read it\n"
        "/indicator AAPL MACD     \u2192 US tickers work too (auto-detected)\n"
        "/indicator RELIANCE      \u2192 the FULL all-indicators card\n"
        "Indicators: rsi \u00b7 macd \u00b7 stochastic \u00b7 bollinger \u00b7 cci \u00b7 adx \u00b7 aroon \u00b7\n"
        "psar \u00b7 supertrend \u00b7 ema/sma \u00b7 gmma \u00b7 vwap \u00b7 atr \u00b7 donchian \u00b7\n"
        "squeeze \u00b7 cmf \u00b7 mfi \u00b7 obv\n"
        "Aliases: /ind, /tech, /technical"
    ),
    "/forecast": (
        "<b>/forecast</b> - the forecast value for a stock\n"
        "/forecast RELIANCE  \u2192 analyst consensus &amp; rating breakdown, target\n"
        "                     price + upside, top executives &amp; competitors\n"
        "/forecast AAPL      \u2192 US stocks work too (auto-detected)\n"
        "Aliases: /analyst, /forecastanalysis"
    ),
    "/learn": (
        "<b>/learn</b> - the detailed guide to EVERY command\n"
        "/learn                \u2192 topic index\n"
        "/learn stocks        \u2192 fundamental analysis group\n"
        "/learn schedule      \u2192 automation group\n"
        "/learn /scan500      \u2192 full walkthrough of ONE command\n"
        "/learn all           \u2192 the entire guide\n"
        "Aliases: /guide, /explain, /tutorial, /howto"
    ),
    "/schedule": (
        "<b>/schedule</b> - your automated reports (per user)\n"
        "/schedule add 3h /scan500          \u2192 every 3 hours\n"
        "/schedule add at 09:15 /toplosers 1h  \u2192 daily at 09:15 IST\n"
        "/schedule add 3h /scan500 us       \u2192 US market hours only (NASDAQ/NYSE)\n"
        "/schedule add 3h /scan500 any      \u2192 no gate - any time\n"
        "/schedule add 3h /cmd in from 09:15 to 15:30  \u2192 explicit run window\n"
        "/schedule pause 1d | 2d | 3d | 1w | 2w | 1mo  \u2192 pause YOUR schedule\n"
        "/schedule resume                   \u2192 resume early\n"
        "/schedule market in|us|any         \u2192 YOUR default market-hours gate\n"
        "/schedule run | /schednow          \u2192 run them all right now\n"
        "/schedule remove 1                 \u2192 remove YOUR entry #1\n"
        "/schedule clear                    \u2192 remove all of yours"
    ),
    "/market": (
        "<b>/market</b> - market-hours gate for YOUR scheduled reports\n"
        "/market in          \u2192 only Indian market hours (NSE/BSE 09:15\u201315:30 IST)\n"
        "/market us          \u2192 only US market hours (NASDAQ/NYSE 09:30\u201316:00 ET)\n"
        "/market any | off   \u2192 no gate - run any time\n"
        "/market             \u2192 live status + your current gate"
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


# One-tap example commands shown as tappable reply-keyboard buttons when a
# bare command displays its usage (typing /toplosers -> buttons for
# /toplosers 1h 10, /toplosers 2d, ...). The button label IS the command
# text, so tapping it runs the command - no typing, on mobile and desktop.
COMMAND_EXAMPLES = {
    "/corpactions": ["/corpactions", "/corpactions dividend", "/corpactions today", "/corpactions RELIANCE"],
    "/exdates": ["/exdates today", "/exdates 7"],
    "/news": ["/news", "/news RELIANCE", "/news 5"],
    "/fundamentalanalyze": ["/fundamentalanalyze RELIANCE", "/fundamentalanalyze mylist", "/fundamentalanalyze 5-10"],
    "/fundamentalreport": ["/fundamentalreport RELIANCE", "/fundamentalreport mylist", "/fundamentalreport 3-5"],
    "/usstock": ["/usstock AAPL", "/usstock MSFT", "/usstock NVDA"],
    "/harmonicpatterns": ["/harmonicpatterns", "/harmonicpatterns 500", "/harmonicpatterns RELIANCE"],
    "/topmovers": ["/topmovers", "/topmovers 1h 10", "/topmovers today 25", "/topmovers 1w 500", "/topmovers 12-08-2026"],
    "/bigmovers": ["/bigmovers", "/bigmovers 8", "/bigmovers 3 nifty100", "/bigmovers 8 sp500"],
    "/topgainers": ["/topgainers", "/topgainers 1h 10", "/topgainers 100", "/topgainers 12-08-2026"],
    "/toplosers": ["/toplosers", "/toplosers 1h 10", "/toplosers 2d", "/toplosers 100", "/toplosers 12-08-2026"],
    "/gappers": ["/gappers", "/gappers 1d", "/gappers 2d", "/gappers window 3d", "/gappers 12-08-2026", "/gappers up", "/gappers all", "/gappers GODREJCP"],
    "/checklist": ["/checklist RELIANCE", "/checklist mylist"],
    "/indicator": ["/indicator RELIANCE RSI", "/indicator AAPL MACD", "/indicator RELIANCE"],
    "/forecast": ["/forecast RELIANCE", "/forecast AAPL", "/forecast GODREJCP"],
    "/learn": ["/learn", "/learn stocks", "/learn schedule"],
    "/schedule": ["/schedule", "/schedule add 3h /toplosers 1h", "/schedule run", "/schedule pause 1d"],
    "/market": ["/market", "/market in", "/market us", "/market any"],
    "/pricealert": ["/pricealert 3", "/pricealert off"],
    "/alertfilters": ["/alertfilters dividend,bonus", "/alertfilters all"],
    "/watcher": ["/watcher on", "/watcher off", "/watcher set 5", "/watcher universe nifty500"],
    "/moversfund": ["/moversfund button", "/moversfund auto"],
    "/addstock": ["/addstock RELIANCE NSE", "/addstock PGINVIT"],
    "/removestock": ["/removestock TCS"],
}


# Commands that already do something useful when typed bare: describe them
# first, then still run them, so the user sees BOTH what it does and the
# result. value = (description, runnable(chat_id)).
DESCRIBE_AND_RUN = {
    "/watchlist": (
        "\U0001F4CB <b>/watchlist</b> - shows YOUR current watchlist with prices. "
        "Add stocks with /addstock, remove with /removestock.",
        lambda chat_id: watchlist_commands.send_watchlist(chat_id),
    ),
    "/settings": (
        "\U00002699\ufe0f <b>/settings</b> - shows YOUR filters, price-alert and watcher "
        "settings. Change them with /alertfilters, /pricealert and /watcher.",
        lambda chat_id: reply(chat_id, format_settings(chat_id)),
    ),
    "/status": (
        "\U0001F4CA <b>/status</b> - where your watchlist is saved, GitHub push status "
        "and your personal data scope.",
        lambda chat_id: status_commands.handle_status(chat_id),
    ),
    "/corpactionssummary": (
        "\U0001F4CA <b>/corpactionssummary</b> - corporate-action snapshot: counts "
        "by exchange &amp; type plus the next ex-dates. Details: /corpactions.",
        lambda chat_id: corporate_action_commands.run_ca_query(chat_id, {"mode": "overview"}),
    ),
    "/corpactionsformylist": (
        "\U0001F4C5 <b>/corpactionsformylist</b> - corporate actions for YOUR "
        "watchlist: upcoming ex-dates plus recently passed / in-progress actions "
        "with status (payment due, rights window, etc.).",
        lambda chat_id: watchlist_commands.send_watchlist_actions(chat_id),
    ),
    "/scan500": (
        "\U0001F50D <b>/scan500</b> - full NIFTY 500 technical scanner: EMAs, SMA, "
        "RSI, MACD, Stochastic, Bollinger, CCI, ADX, Aroon, PSAR, CMF, MFI, OBV, "
        "Supertrend, GMMA, VWAP &amp; Mansfield RS, then a full indicator card for "
        "each of the TOP 10. Takes ~1-2 min - starting it now.",
        lambda chat_id: scanner_commands.handle_scan500(chat_id, ["/scan500"]),
    ),
    "/checknow": (
        "\u26A1 <b>/checknow</b> - force-runs an alert check now and re-sends "
        "every matching alert (corporate actions, reminders, price moves).",
        lambda chat_id: reply(
            chat_id,
            "Running a forced check now - re-sending all matching alerts shortly.",
        ),
    ),
}


def _bare_command_usage(chat_id, command) -> bool:
    """When a main command is typed with no arguments, explain it.

    Commands with subcommands get the subcommand list (COMMAND_USAGE);
    commands that already produce useful output get a short description AND
    are still run (DESCRIBE_AND_RUN), so nothing useful is lost.
    Returns True when a hint was sent.
    """
    usage = COMMAND_USAGE.get(command)
    if usage:
        # CURRENT status first (e.g. watcher ON/OFF, current filters, your
        # schedule), then the subcommand list - so nothing is lost. One-tap
        # example buttons ride along so every command can be run without
        # typing (mobile + desktop).
        status_fn = COMMAND_STATUS.get(command)
        status = status_fn(chat_id) if status_fn else ""
        examples = COMMAND_EXAMPLES.get(command)
        recent = storage.get_recent_commands(chat_id)
        # One-tap buttons: tap to RUN the command (callback-based, works on
        # mobile and desktop). The chat's recent commands are appended so the
        # user can re-run what they actually use with one tap.
        reply_markup = None
        if examples or recent:
            rows = list(
                (inline_command_buttons(examples).get("inline_keyboard") or [])
                if examples else []
            )
            if recent:
                rows.extend(recent_buttons(recent))
            reply_markup = {"inline_keyboard": rows}
        reply(
            chat_id,
            (status + "\n\n" if status else "") + usage,
            reply_markup=reply_markup,
        )
        return True
    described = DESCRIBE_AND_RUN.get(command)
    if described:
        reply(chat_id, described[0])
        described[1](chat_id)
        return True
    return False


def _primary_examples() -> list[str]:
    """One representative example per command, in help order (for /help)."""
    primaries = []
    for command in COMMAND_USAGE:
        examples = COMMAND_EXAMPLES.get(command)
        if examples:
            primaries.append(examples[0])
    return primaries


def send_help(chat_id):
    """Send the styled HTML help message (/help, /start, unknown commands).

    Rides along a blue grid of one-tap command buttons (one example per
    command) plus the persistent quick-menu reply keyboard, so every
    command in the help is copyable in a single tap.
    """
    from .help_texts import HELP_TEXT

    markup = quick_menu_markup()
    grid = inline_command_buttons(_primary_examples(), per_row=2)
    rows = list((grid.get("inline_keyboard") or []) if grid else [])
    recent = storage.get_recent_commands(chat_id)
    if recent:
        rows.extend(recent_buttons(recent))
    if rows:
        markup["inline_keyboard"] = rows
    reply_messages(
        chat_id,
        split_messages(HELP_TEXT.split("\n")),
        reply_markup=markup,
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
        {"command": "usstock", "description": "US stock details: /usstock AAPL (USD fundamentals)"},
        {"command": "checklist", "description": "32-point investment scorecard: /checklist RELIANCE"},
        {"command": "harmonicpatterns", "description": "Harmonic pattern scan NIFTY 100/500: /harmonicpatterns all"},
        {"command": "indicator", "description": "One-indicator deep-dive: /indicator RELIANCE RSI (US works too)"},
        {"command": "forecast", "description": "Analyst forecast + executives + competitors: /forecast RELIANCE"},
        {"command": "scan500", "description": "NIFTY 500 CNC/MIS technical scanner"},
        {"command": "topmovers", "description": "Top gainers AND losers with fundamentals"},
        {"command": "topgainers", "description": "Top rising stocks with fundamentals"},
        {"command": "toplosers", "description": "Top falling stocks with fundamentals"},
        {"command": "gappers", "description": "Overnight gaps (prev close vs today's open): /gappers down, /gappers GODREJCP"},
        {"command": "alertfilters", "description": "Receive only chosen action types"},
        {"command": "pricealert", "description": "Alert on +/-PCT% daily price move"},
        {"command": "settings", "description": "Show your current settings"},
        {"command": "schedule", "description": "Auto reports: /schedule add 3h /scan500, pause 1d, market us"},
        {"command": "market", "description": "Market-hours gate for reports: /market us, in, any"},
        {"command": "status", "description": "Check persistence / GitHub push"},
        {"command": "checknow", "description": "Force a check and resend alerts"},
        {"command": "watcher", "description": "Big-move alerts: /watcher on, off, set 5, universe nifty500"},
        {"command": "moversfund", "description": "Movers: button vs auto fundamentals"},
        {"command": "all", "description": "Show every command - copy & send any line"},
        {"command": "menu", "description": "One-tap command buttons - no typing"},
        {"command": "schednow", "description": "Run all your scheduled commands right now"},
        {"command": "learn", "description": "Detailed guide to every command: /learn stocks, /learn /scan500"},
        {"command": "help", "description": "Show all commands and examples"},
    ]
    return set_my_commands(menu)
