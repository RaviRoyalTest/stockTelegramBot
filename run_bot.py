"""Entry point for running in a GitHub Actions cron job.

Two jobs, one run:
  1. Optionally process Telegram bot commands (/addstock, /removestock, /watchlist, /help)
     when PROCESS_COMMANDS=true (default). Set PROCESS_COMMANDS=false in the
     GitHub Actions cron so the always-on bot server is the only process that
     polls getUpdates (avoids double replies and 409 conflicts). Any change
     is committed and pushed back to the repo using GH_TOKEN.
  2. Run one poll cycle: fetch corporate actions, filter to the watchlist,
     and send new ones to Telegram.

Local usage:  python run_bot.py
"""
import logging
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from time import monotonic
from pathlib import Path

from corp_actions import config  # no third-party deps - always importable

try:
    import requests

    import corp_actions.poller as poller_mod
    from corp_actions import harmonic, notifier, sources, storage
    from corp_actions.poller import poller
except ImportError:
    # The dependency-light --check diagnostic must still run when
    # requirements.txt hasn't been installed yet. Anything that actually
    # needs the missing deps fails later with a clear error.
    if not any(a.lower() == "--check" for a in sys.argv[1:]):
        raise
    print(
        "Note: some dependencies are missing - running in dependency-light "
        "diagnostic mode. Install them for the full bot: "
        "pip install -r requirements.txt",
        file=sys.stderr,
    )


class _ImmediateStreamHandler(logging.StreamHandler):
    """Flush after every record so Render / PaaS logs appear immediately.

    When stdout is piped (not a TTY - the norm on Render), Python enables
    block buffering, so logs written with the default StreamHandler sit in
    the buffer and Render shows nothing for a long time. Flushing on every
    emit makes each log line appear in the dashboard right away.
    """

    def emit(self, record):
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    handlers=[_ImmediateStreamHandler(sys.stdout)],
)
log = logging.getLogger("run_bot")

import html

HELP_TEXT = (
    "\U0001F4CA <b>Stock Alert Bot \u2014 Command Guide</b>\n"
    "<i>Real-time NSE/BSE corporate actions, market movers &amp; news</i>\n\n"
    "Every command name explains what it does. The old short forms still work\n"
    "as aliases (e.g. <code>/ca</code> = <code>/corpactions</code>, "
    "<code>/next</code> = <code>/corpactionsformylist</code>).\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F3A8 <b>Colour &amp; Signal Legend</b>\n"
    "\U0001F7E2\u25b2 Green + Up arrow = Stock gaining\n"
    "\U0001F534\u25bc Red + Down arrow  = Stock falling\n"
    "\u2705 <b>Strong Buy</b> = Near 52-week LOW (\u226415% from bottom)\n"
    "\U0001F4C8 <b>Buy Zone</b>  = Low zone (15\u201335% from bottom)\n"
    "\U0001F7E1 <b>Mid-Range</b> = Middle of 52-week range\n"
    "\u26a0\ufe0f <b>High Zone</b>  = Near 52-week HIGH (65\u201385%)\n"
    "\U0001F6AB <b>Avoid</b>     = At/near 52-week HIGH (\u226585%)\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4C5 <b>Corporate Actions (NSE + BSE)</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/corpactions <i>[TYPE | SYMBOL | N | today]</i>\n"
    "  Browse all dividend / bonus / split / rights / buyback actions.\n"
    "  /corpactions                  \u2192 overview of everything upcoming\n"
    "  /corpactions dividend         \u2192 only dividend announcements\n"
    "  /corpactions bonus|split|rights|buyback  \u2192 one action type\n"
    "  /corpactions increase         \u2192 shareholder increase (bonus+split+rights)\n"
    "  /corpactions today            \u2192 ex-dates due today\n"
    "  /corpactions 7                \u2192 ex-dates within the next 7 days\n"
    "  /corpactions RELIANCE         \u2192 full details for one symbol\n"
    "  /corpactions TATA             \u2192 keyword search (company/subject)\n\n"
    "/exdates <i>[today|N]</i>       \u2192 all actions by ex-date window\n"
    "  (default 5 days) \u00b7  /exdates today  \u00b7  /exdates 10\n\n"
    "/corpactionssummary  \u2192 corporate-action snapshot: counts by exchange\n"
    "                       &amp; type, plus the next ex-dates\n\n"
    "/corpactionsformylist \u2192 YOUR watchlist: upcoming ex-dates PLUS recently\n"
    "                       passed / in-progress actions with status (rights\n"
    "                       subscription open, dividend payment due/pending,\n"
    "                       bonus credit) \u2014 last 30 days\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\u2B50 <b>Watchlist</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/watchlist           \u2192 show your full watchlist\n"
    "/myfavourites        \u2192 run your favourite commands in one go:\n"
    "                       corporate actions for your list, top losers (1h +\n"
    "                       today), watchlist &amp; fundamentals for your stocks\n"
    "/addstock SYMBOL [NSE|BSE] \u2192 add a stock (default NSE)\n"
    "  /addstock RELIANCE NSE  \u00b7  /addstock PGINVIT\n"
    "/removestock SYMBOL  \u2192 remove a stock from your watchlist\n"
    "  /removestock TCS\n"
    "/news <i>[N|SYMBOL]</i> \u2192 latest headlines for your watchlist stocks\n"
    "  /news              \u2192 news for all watchlist stocks\n"
    "  /news 5            \u2192 5 headlines per stock\n"
    "  /news RELIANCE     \u2192 news for RELIANCE only\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F50D <b>Stock Analysis</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/fundamentalanalyze <i>SYMBOL</i>  \u2192 quick analysis card\n"
    "  /fundamentalanalyze TATATECH  \u2192 price, P/E, 52W signal, QoQ shareholding\n"
    "/fundamentalanalyze <i>N | N-M | mylist</i>  \u2192 same card for your stocks\n"
    "  /fundamentalanalyze mylist  \u2192 your whole watchlist (10 per page, Next button)\n"
    "  /fundamentalanalyze 5-10    \u2192 watchlist stocks #5 to #10\n\n"
    "/fundamentalreport <i>SYMBOL</i>  \u2192 DEEP fundamental report\n"
    "  (much more detailed than /fundamentalanalyze)\n"
    "  /fundamentalreport RELIANCE  \u2192 valuation, growth, margins, balance sheet,\n"
    "                     EPS, analyst targets &amp; shareholding\n"
    "  /fundamentalreport 3-5   \u2192 deep report for watchlist #3..#5\n"
    "  /fundamentalreport mylist \u2192 deep report for your whole watchlist (5 per page)\n\n"
    "/harmonicpatterns <i>[all|100|500] [TIMEFRAME]</i>  \u2192 harmonic patterns\n"
    "  Scans for Gartley / Bat / Butterfly / Crab / Shark setups.\n"
    "  /harmonicpatterns all  \u2192 NIFTY 100, daily \u00b7  /harmonicpatterns 500 1w\n"
    "  /harmonicpatterns RELIANCE  \u2192 full report with PRZ, entry, SL &amp; targets\n"
    "  Timeframes: 5m 15m 30m 1h 4h 1d 1w (alias /harmonic)\n\n"
    "/scan500             \u2192 full NIFTY 500 CNC/MIS technical scanner\n"
    "  EMAs, RSI, MACD, ADX, CMF, OBV, Aroon, TTM Squeeze, Supertrend, GMMA,\n"
    "  VWAP &amp; Mansfield RS \u2014 scores survivors /100, picks the #1 setup and\n"
    "  maps an hour-by-hour CNC vs MIS execution plan (takes ~1-2 min).\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4C8 <b>Market Screens</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/topmovers <i>[period] [N] [100|500]</i>  \u2192 top gainers AND losers\n"
    "  /topmovers          \u2192 last 1h, NIFTY 100\n"
    "  /topmovers 2d 500   \u2192 2-day movers, NIFTY 500\n"
    "  /topmovers 1w 10    \u2192 top 10 movers this week\n\n"
    "/topgainers <i>[period] [N] [100|500]</i>  \u2192 top rising stocks\n"
    "  /topgainers 1h          \u2192 last 1h gainers\n"
    "  /topgainers 1mo 20 500  \u2192 top 20 gainers this month, NIFTY 500\n\n"
    "/toplosers <i>[period] [N] [100|500]</i>  \u2192 top falling stocks\n"
    "  /toplosers 1h 10       \u2192 top 10 losers last hour\n"
    "  /toplosers 1w nifty100 \u2192 weekly losers, NIFTY 100\n\n"
    "Periods: 5m \u00b7 15m \u00b7 30m \u00b7 1h \u00b7 2h \u00b7 4h \u00b7 today \u00b7 1d \u00b7 2d \u00b7 5d \u00b7 1w \u00b7 2w \u00b7 1mo \u00b7 3mo \u00b7 6mo \u00b7 1y\n"
    "Universe: n100/nifty100=NIFTY 100 \u00b7 n500/nifty500=NIFTY 500\n"
    "Tip: for /topgainers &amp; /toplosers a bare 100/500 means the top-N count\n"
    "(e.g. /topgainers 100 = top 100) - use nifty100/nifty500 or a second\n"
    "number for the index. For /topmovers a bare 100/500 picks the index.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\u2699\ufe0f <b>Alerts &amp; Personalisation</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/alertfilters TYPE,TYPE  \u2192 receive only the action types you choose\n"
    "  /alertfilters dividend,bonus  \u00b7  /alertfilters all (reset)\n"
    "  Types: dividend \u00b7 bonus \u00b7 split \u00b7 rights \u00b7 buyback\n"
    "/pricealert PCT      \u2192 alert when a stock moves \u00b1PCT% in a day\n"
    "  /pricealert 3      \u2192 alert on \u00b13% move  \u00b7  /pricealert off (disable)\n"
    "/settings            \u2192 view your current filter &amp; alert config\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F6E0 <b>System</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/status              \u2192 YOUR personal setup (watchlist, schedule, settings, alerts)\n"
    "/schedule add 3h /scan500  \u2192 YOUR report runs /scan500 automatically every 3h\n"
    "/checknow            \u2192 force-run alerts and re-send all matches\n"
    "/watcher             \u2192 big-move alerts: /watcher on, off, set 5, universe nifty500\n"
    "/menu                \u2192 one-tap command buttons (tap, no typing)\n"
    "/help \u00b7 /start       \u2192 show this guide\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4A1 <b>Quick Examples</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/topgainers 1h 10          \u2192 Top 10 gainers last hour\n"
    "/toplosers 1mo nifty500   \u2192 Monthly losers \u2014 NIFTY 500\n"
    "/topmovers 2d 10 500       \u2192 2-day movers, top 10, NIFTY 500\n"
    "/scan500               \u2192 full NIFTY 500 CNC/MIS technical scan\n"
    "/corpactions dividend   \u2192 Upcoming dividends\n"
    "/corpactions RELIANCE   \u2192 RELIANCE corporate actions\n"
    "/corpactionsformylist  \u2192 Watchlist ex-dates + in-progress actions\n"
    "/myfavourites          \u2192 All your regular commands in one go\n"
    "/addstock INFY NSE      \u2192 Add INFY to watchlist\n"
    "/news RELIANCE          \u2192 Latest RELIANCE headlines\n"
    "/alertfilters bonus,split  \u2192 Only bonus &amp; split alerts\n"
    "/pricealert 2.5         \u2192 Alert on \u00b12.5% daily move\n"
    "\U0001F4DD Type in plain text: \"gainers\", \"dividends\", \"ex-date today\", \"news\""
)

CA_HELP = (
    "Corporate Action queries (/corpactions, alias /ca):\n"
    "/corpactions - overview of all NSE + BSE actions\n"
    "/corpactions dividend | bonus | split | rights | buyback - one action type\n"
    "/corpactions increase - shareholder increase (bonus + split + rights)\n"
    "/corpactions today - ex-date today\n"
    "/corpactions 7 - ex-date within 7 days\n"
    "/corpactions RELIANCE - details for one symbol\n"
    "/corpactions TATA - keyword search in company name / subject"
)


# --------------------------------------------------------------------- telegram
def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    log.info("getUpdates(offset=%s) -> %d update(s)", offset, len(updates))
    return updates


def reply(chat_id, text, parse_mode="HTML", reply_markup=None):
    """Send a message to a chat, splitting into chunks if text exceeds Telegram limits.

    `reply_markup` is an optional Telegram keyboard dict (see /menu).
    """
    if len(text) > 3800:
        msgs = _split_messages(text.split("\n"))
        _reply_messages(chat_id, msgs, reply_markup=reply_markup)
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        log.info(
            "reply to chat %s: %d chars -> %s",
            chat_id, len(text), text[:100].replace("\n", " "),
        )
    except Exception as exc:
        if parse_mode:
            payload.pop("parse_mode", None)
            try:
                resp = requests.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
                resp.raise_for_status()
                log.info("reply (plain) to chat %s: %d chars", chat_id, len(text))
                return
            except Exception:
                pass
        log.warning(
            "reply to chat %s failed: %s (text: %s)",
            chat_id, config.redact(exc), text[:100].replace("\n", " "),
        )


# When a main command is typed with NO arguments, show its subcommands and
# examples (like /watcher does) instead of silently running a default. Each
# command's bare form still works for anyone who prefers the default - this
# is only a help hint layer, it never changes what an argument-ful command
# does.
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
        "\U0001F6A8 <b>Sudden-move watcher</b>\n"
        "/watcher on    \u2192 turn it ON\n"
        "/watcher off   \u2192 turn it OFF\n"
        "/watcher set 3 \u2192 alert at a 3% session move\n"
        "/watcher universe nifty500 \u2192 nifty100 | nifty500 | mylist\n"
        "/watcher       \u2192 show current status"
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
    "/menu": (
        "<b>/menu</b> - one-tap command buttons (no typing)\n"
        "/menu       \u2192 show the quick menu\n"
        "/menu off   \u2192 hide the quick menu"
    ),
}


# Commands that already do something useful when typed bare: describe them
# first, then still run them, so the user sees BOTH what it does and the
# result. value = (description, runnable(chat_id)).
DESCRIBE_AND_RUN = {
    "/watchlist": (
        "\U0001F4CB <b>/watchlist</b> - shows YOUR current watchlist with prices. "
        "Add stocks with /addstock, remove with /removestock.",
        lambda cid: send_watchlist(cid),
    ),
    "/settings": (
        "\U00002699\ufe0f <b>/settings</b> - shows YOUR filters, price-alert and watcher "
        "settings. Change them with /alertfilters, /pricealert and /watcher.",
        lambda cid: reply(cid, format_settings(cid)),
    ),
    "/status": (
        "\U0001F4CA <b>/status</b> - where your watchlist is saved, GitHub push status "
        "and your personal data scope.",
        lambda cid: handle_status(cid),
    ),
    "/myfavourites": (
        "\u2B50 <b>/myfavourites</b> - runs your regular commands in ONE go: "
        "corporate actions for your list, top losers (1h + today), your watchlist "
        "and deep fundamentals for your stocks.",
        lambda cid: handle_favourites(cid),
    ),
    "/corpactionssummary": (
        "\U0001F4CA <b>/corpactionssummary</b> - corporate-action snapshot: counts "
        "by exchange &amp; type plus the next ex-dates. Details: /corpactions.",
        lambda cid: run_ca_query(cid, {"mode": "overview"}),
    ),
    "/corpactionsformylist": (
        "\U0001F4C5 <b>/corpactionsformylist</b> - corporate actions for YOUR "
        "watchlist: upcoming ex-dates plus recently passed / in-progress actions "
        "with status (payment due, rights window, etc.).",
        lambda cid: send_watchlist_actions(cid),
    ),
    "/scan500": (
        "\U0001F50D <b>/scan500</b> - full NIFTY 500 technical scanner (EMAs, RSI, "
        "MACD, ADX, Supertrend, VWAP...). Takes ~1-2 min - starting it now.",
        lambda cid: handle_scan500(cid, ["/scan500"]),
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
        reply(chat_id, usage)
        return True
    described = DESCRIBE_AND_RUN.get(cmd)
    if described:
        reply(chat_id, described[0])
        described[1](chat_id)
        return True
    return False


def send_help(chat_id):
    """Send the styled HTML help message (/help, /start, unknown commands)."""
    _reply_messages(
        chat_id,
        _split_messages(HELP_TEXT.split("\n")),
        reply_markup=notifier.quick_menu_markup(),
    )


def github_push_configured() -> bool:
    """True only when the host can actually push state back to GitHub."""
    return bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


# ----------------------------------------------------------------- watchlist
def handle_command(chat_id, text):
    parts = (text or "").strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    log.info("command from chat %s: %s", chat_id, text)

    if cmd in ("/start", "/help", "/"):
        send_help(chat_id)
        return

    # Bare main command -> list its subcommands (like /watcher does). Only
    # fires when NO arguments were given, so nothing with arguments changes.
    if len(parts) == 1 and _bare_command_usage(chat_id, cmd):
        return

    if cmd in ("/list", "/watchlist"):
        send_watchlist(chat_id)
        return

    if cmd == "/checknow":
        reply(chat_id, "Running a forced check now - re-sending all matching alerts shortly.")
        return

    if cmd in ("/next", "/upcoming", "/corpactionsformylist"):
        send_watchlist_actions(chat_id)
        return

    if cmd in ("/filter", "/alertfilters", "/actionfilters"):
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
        bad = []
        if raw in ("all", "off", "none", "-"):
            chosen = []
        else:
            chosen = []
            for token in raw.split(","):
                token = token.strip()
                if token in sources.ACTION_TYPES:
                    chosen.append(token)
                elif token:
                    bad.append(token)
        settings["action_filters"] = chosen
        storage.save_user_settings(chat_id, settings)
        log.info(
            "chat %s filters set to: %s",
            chat_id, ", ".join(chosen) if chosen else "all types",
        )
        msg = "Filters set to: <b>" + html.escape(", ".join(chosen) if chosen else "all types") + "</b>"
        if bad:
            msg += f"\nIgnored unknown type(s): {html.escape(', '.join(bad))}"
            msg += f" (valid: {', '.join(sources.ACTION_TYPES)})"
        reply(chat_id, msg)
        return

    if cmd in ("/alert", "/pricealert"):
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
            val = None
        else:
            try:
                val = abs(float(raw.strip().rstrip("%")))
            except ValueError:
                reply(chat_id, "Usage: <code>/alert 3</code> (e.g. 3%) or <code>/alert off</code>")
                return
            if val == 0:
                val = None
        settings["price_alert_pct"] = val
        storage.save_user_settings(chat_id, settings)
        log.info(
            "chat %s price-alert threshold set to: %s",
            chat_id, "off" if val is None else f"{val:g}%",
        )
        reply(chat_id, f"Price alerts {'off' if val is None else 'set to <b>' + format(val, 'g') + '%</b>'}.")
        return

    if cmd in ("/watcher", "/bigmover", "/moverwatch"):
        settings = storage.get_user_settings(chat_id)
        watcher = settings.get("watcher") or {}
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub in ("on", "enable", "start"):
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
        if sub in ("off", "disable", "stop"):
            watcher["enabled"] = False
            settings["watcher"] = watcher
            storage.save_user_settings(chat_id, settings)
            reply(chat_id, "\U0001F6A8 <b>Sudden-move watcher OFF.</b> No more big-move alerts.")
            return
        if sub in ("set", "threshold", "pct"):
            if len(parts) < 3:
                reply(chat_id, "Usage: <code>/watcher set 5</code> (percent move, e.g. 5 = 5%)")
                return
            try:
                val = abs(float(parts[2].strip().rstrip("%")))
            except ValueError:
                reply(chat_id, "Usage: <code>/watcher set 5</code> (percent move)")
                return
            if val == 0:
                val = 5.0
            watcher["threshold"] = val
            watcher["enabled"] = bool(watcher.get("enabled"))
            settings["watcher"] = watcher
            storage.save_user_settings(chat_id, settings)
            reply(chat_id, f"Watcher threshold set to <b>{val:g}%</b> "
                  f"({"ON" if watcher.get("enabled") else "OFF"} - use <code>/watcher on</code> to enable).")
            return
        if sub in ("universe", "scope", "market"):
            if len(parts) < 3:
                reply(chat_id, "Usage: <code>/watcher universe nifty100</code> | nifty500 | mylist")
                return
            u = parts[2].lower()
            if u in ("nifty100", "n100", "100"):
                u = "nifty100"
            elif u in ("nifty500", "n500", "500", "all"):
                u = "nifty500"
            elif u in ("mylist", "watchlist", "list", "my"):
                u = "mylist"
            else:
                reply(chat_id, "Universe must be <code>nifty100</code>, <code>nifty500</code> or <code>mylist</code>.")
                return
            watcher["universe"] = u
            watcher["enabled"] = bool(watcher.get("enabled"))
            settings["watcher"] = watcher
            storage.save_user_settings(chat_id, settings)
            reply(chat_id, f"Watcher universe set to <b>{u.upper()}</b> "
                  f"({"ON" if watcher.get("enabled") else "OFF"} - use <code>/watcher on</code> to enable).")
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
        return

    if cmd == "/status":
        handle_status(chat_id)
        return

    if cmd in ("/ca", "/corpactions", "/corporate-actions", "/corp-actions", "/actions", "/shareholder", "/increase"):
        if cmd in ("/shareholder", "/increase"):
            descriptor = {"mode": "types", "types": list(sources.INCREASE_TYPES)}
        elif len(parts) > 1:
            descriptor = _parse_ca_arg(parts[1])
        else:
            descriptor = {"mode": "overview"}
        if descriptor is None:
            reply(chat_id, CA_HELP)
        else:
            run_ca_query(chat_id, descriptor)
        return

    if cmd in ("/exdate", "/exdates", "/ex-dates"):
        days = config.REMINDER_DAYS
        if len(parts) > 1:
            if parts[1].lower() == "today":
                days = 0
            else:
                try:
                    days = max(0, int(parts[1]))
                except ValueError:
                    reply(chat_id, "Usage: /exdate today  or  /exdate 7")
                    return
        run_ca_query(chat_id, {"mode": "exdate", "days": days})
        return

    if cmd in ("/summary", "/casummary", "/corpactionssummary"):
        run_ca_query(chat_id, {"mode": "overview"})
        return

    if cmd == "/settings":
        reply(chat_id, format_settings(chat_id))
        return

    if cmd in ("/myfavourites", "/favorites", "/favourites", "/mypicks", "/dailybrief"):
        handle_favourites(chat_id)
        return

    if cmd in ("/menu", "/quick", "/shortcuts", "/buttons"):
        handle_menu(chat_id, parts)
        return

    if cmd in ("/sched", "/schedule"):
        handle_sched(chat_id, parts)
        return

    if cmd == "/news":
        handle_news(chat_id, parts)
        return

    if cmd in ("/movers", "/topmovers", "/marketmovers"):
        handle_movers(chat_id, parts)
        return

    if cmd in ("/gainers", "/topgainers", "/losers", "/toplosers"):
        direction = "gainers" if cmd in ("/gainers", "/topgainers") else "losers"
        handle_gainers_losers(chat_id, parts, direction)
        return

    if cmd in ("/fund", "/fundamentals", "/fundamentalreport"):
        handle_fund_analysis(chat_id, parts)
        return

    if cmd in ("/harmonic", "/harmonicpatterns"):
        handle_harmonic(chat_id, parts)
        return

    if cmd == "/scan500":
        handle_scan500(chat_id, parts)
        return

    if cmd in ("/stock", "/info", "/quote", "/stockanalysis", "/stock-analysis", "/analysis", "/fundamentalanalyze", "/fundamental-analysis"):
        handle_single_stock_analysis(chat_id, parts)
        return

    if len(parts) < 2:
        if cmd in ("/add", "/addstock", "/remove", "/removestock"):
            reply(chat_id, "Usage: <code>/addstock SYMBOL [NSE|BSE]</code> or <code>/removestock SYMBOL [NSE|BSE]</code>")
        else:
            reply(chat_id, f"Unknown command <code>{html.escape(cmd)}</code>. Type <code>/help</code> for the available commands.")
        return

    raw_symbol = parts[1].upper().strip()
    if raw_symbol.endswith(".BO"):
        symbol = raw_symbol.removesuffix(".BO")
        exchange = "BSE"
    elif raw_symbol.endswith(".NS"):
        symbol = raw_symbol.removesuffix(".NS")
        exchange = "NSE"
    else:
        symbol = raw_symbol
        exchange = (parts[2].upper().strip() if len(parts) > 2 else "NSE")
        exchange = exchange if exchange in ("NSE", "BSE") else "NSE"

    if cmd in ("/add", "/addstock"):
        quote = sources.get_quote(exchange, symbol)
        company = quote.get("name", "") if quote else ""
        validated = quote is not None
        if not validated and exchange == "NSE":
            exact = next(
                (
                    s for s in sources.search_stocks(symbol, limit=5)
                    if s["symbol"].upper() == symbol
                ),
                None,
            )
            if exact is not None:
                company = exact.get("company", "")
                validated = True
                log.info(
                    "Yahoo quote unavailable for %s:%s; validated via NSE stock list",
                    exchange, symbol,
                )
        elif not validated and exchange == "BSE":
            try:
                bse_list = sources.get_bse_stock_list()
                exact = next(
                    (s for s in bse_list if s["symbol"].upper() == symbol or s.get("code") == symbol),
                    None,
                )
                if exact is not None:
                    symbol = exact["symbol"]
                    company = exact.get("company", "")
                    validated = True
            except Exception:
                pass

        if not validated:
            _reply_suggestions(chat_id, symbol)
            return
        storage.add_to_user_list(
            chat_id,
            {"symbol": symbol, "company": company, "exchange": exchange},
        )
        where = (
            "watchlist.json (owner's list)"
            if storage.is_owner(chat_id)
            else f"subscriptions.json (chat {chat_id})"
        )
        log.info("Added %s (%s) for chat %s -> %s", symbol, exchange, chat_id, where)
        reply(
            chat_id,
            f"Added <b>{symbol}</b> ({exchange}). Alerts will come to this chat.\n"
            f"Saved in: <code>{html.escape(where)}</code>.",
        )
    elif cmd in ("/remove", "/removestock"):
        storage.remove_from_user_list(chat_id, symbol, exchange)
        log.info("Removed %s (%s) for chat %s", symbol, exchange, chat_id)
        reply(chat_id, f"Removed <b>{symbol}</b> ({exchange}) if it was present.")
    else:
        send_help(chat_id)


def send_watchlist(chat_id) -> None:
    """Show the requester's full watchlist (/watchlist, /list)."""
    items = storage.get_user_list(chat_id)
    if not items:
        reply(chat_id, "Your watchlist is empty.")
        return
    lines = [
        f"{idx}. <b>{i['symbol']}</b> ({i['exchange']})"
        for idx, i in enumerate(items, start=1)
    ]
    where = (
        "watchlist.json (owner's list)"
        if storage.is_owner(chat_id)
        else f"subscriptions.json (your chat {chat_id})"
    )
    persistence = (
        "pushed to GitHub - it survives redeploys."
        if github_push_configured()
        else "NOT pushed to GitHub - it is only on this host's disk "
        "and WILL BE LOST on redeploy. Run /status to confirm."
    )
    # Cross-link: tap a ticker below to open its fundamentals immediately
    tap_symbols = [i["symbol"] for i in items[:12]]
    reply(
        chat_id,
        "<b>Your Watchlist:</b>\n"
        + "\n".join(lines)
        + f"\n\nUse <code>/fundamentalanalyze 5-10</code> or <code>/fundamentalreport 3-5</code> to get details by these numbers."
        + "\nTap a ticker below for its fundamentals."
        + f"\nSaved in: <code>{html.escape(where)}</code>\nPersistence: {html.escape(persistence)}",
        reply_markup=notifier.symbol_buttons(tap_symbols, "fund") if tap_symbols else None,
    )


def send_watchlist_actions(chat_id) -> None:
    """Corporate actions for the requester's watchlist with status
    (/corpactionsformylist, /next, /upcoming).
    """
    items = storage.get_user_list(chat_id)
    if not items:
        reply(chat_id, "Your watchlist is empty.")
        return
    try:
        matching = poller_mod.fetch_matching(items)
    except Exception as exc:
        reply(chat_id, f"Could not fetch corporate actions: {html.escape(config.redact(str(exc)))}")
        return
    upcoming = [
        a for a in matching if poller_mod.within_reminder_window(a.get("ex_date"))
    ]
    recent = [
        a for a in matching if poller_mod.recently_passed(a.get("ex_date"))
    ]
    pending = [
        a for a in matching if not poller_mod.parse_ex_date(a.get("ex_date"))
    ]
    # Attach live prices so each colorful block can show the current price.
    for group in (upcoming, recent, pending):
        _attach_quotes(group)
    # Cross-link: one tappable button per symbol -> deep fundamentals
    seen, tap_symbols = set(), []
    for a in upcoming + recent + pending:
        sym = (a.get("symbol") or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            tap_symbols.append(sym)
    reply(
        chat_id,
        notifier.format_next_report(upcoming, recent, pending),
        reply_markup=notifier.symbol_buttons(tap_symbols[:12], "fund") if tap_symbols else None,
    )


def handle_status(chat_id) -> None:
    """Render the /status report: role, where your data lives, GitHub push."""
    gh_configured = github_push_configured()
    owner = storage.is_owner(chat_id)
    location = (
        "watchlist.json (the owner's list)"
        if owner
        else f"subscriptions.json (your chat {chat_id})"
    )
    if gh_configured:
        branch = _push_branch("")
        pending = pending_state_changes()
        push_status = (
            f"configured - changes are pushed to GitHub (branch {branch}) "
            "after each command"
        )
        sync_line = (
            "Local state vs GitHub: "
            + (pending or "in sync (nothing uncommitted)")
        )
        if push_error:
            push_status += " - last push FAILED"
            sync_line += f" (last error: {push_error})"
    else:
        push_status = (
            "NOT set - your changes stay only on this host's disk (lost "
            "on redeploy). Set GH_TOKEN + GITHUB_REPOSITORY on this host."
        )
        sync_line = "Local state vs GitHub: unknown (no GitHub credentials)"
    personal_line = (
        "<b>Personal:</b> everything here is yours alone - your watchlist, "
        "schedule, settings and alerts. Other users' data never mixes "
        "with yours and they cannot touch yours."
    )
    reply(
        chat_id,
        "\n".join(
            [
                f"<b>Your chat id:</b> <code>{chat_id}</code>",
                f"<b>Role:</b> {'owner' if owner else 'subscriber'}",
                personal_line,
                f"<b>Saved in:</b> <code>{html.escape(location)}</code>",
                f"<b>GitHub push:</b> {html.escape(push_status)}",
                html.escape(sync_line),
                f"<b>Scheduled reports:</b> "
                + ("enabled" if config.SCHEDULED_REPORTS_ENABLED and config.PROCESS_COMMANDS else "off")
                + " \u00b7 " + html.escape(format_schedule(chat_id).split("\n")[0])
                + f" \u00b7 manage with /schedule",
                "Run /watchlist to see your current watchlist.",
            ]
        ),
    )


def handle_favourites(chat_id) -> None:
    """Run the user's favourite / regular commands in one go (/myfavourites).

    Bundle: corporate actions for the watchlist, top losers (1h + today),
    the watchlist itself, and deep fundamentals for the watchlist.
    """
    reply(
        chat_id,
        "\U0001F4CB <b>Your Favourites</b> - running your regular commands...",
    )
    send_watchlist_actions(chat_id)
    handle_gainers_losers(chat_id, ["/toplosers", "1h"], "losers")
    handle_gainers_losers(chat_id, ["/toplosers", "1d", "10"], "losers")
    send_watchlist(chat_id)
    handle_fund_analysis(chat_id, ["/fundamentalreport", "mylist"])
    reply(
        chat_id,
        "\u2705 <b>Favourites done.</b> Use <code>/menu</code> for one-tap commands "
        "or <code>/myfavourites</code> to run this again.",
    )


def _close_symbols(query: str, limit: int = 3) -> list[str]:
    """Fuzzy NSE symbol suggestions via difflib (e.g. 'gensys' -> GENESYS).

    Exact substring search fails on typos and symbol-vs-company-name
    mismatches, so fall back to close matches from the full NSE list.
    """
    try:
        from difflib import get_close_matches

        stocks = sources.get_nse_stock_list_cached()
        symbols = [s["symbol"] for s in stocks]
    except Exception:
        return []
    return get_close_matches((query or "").upper(), symbols, n=limit, cutoff=0.72)


def _reply_suggestions(chat_id, query, cmd="add"):
    """Reply with matching stocks from the NSE list when an exact symbol fails.

    cmd is the command the user actually ran (add|stock|fund|harmonic) so the
    suggested follow-up reuses it instead of always suggesting /add.
    """
    matches = sources.search_stocks(query, limit=10)
    if not matches:
        log.info(
            "No stock matched '%s' for chat %s - nothing added", query, chat_id
        )
        reply(chat_id, f"No stocks match '{query}'.")
        return
    lines = [f"'{notifier.escape(query)}' not found as an exact symbol. Did you mean (NSE):"]
    for m in matches:
        company = m["company"] or ""
        if cmd == "add":
            lines.append(f"  /addstock {m['symbol']} NSE  - {notifier.escape(company)}")
        else:
            lines.append(f"  /{cmd} {m['symbol']}  - {notifier.escape(company)}")
    reply(chat_id, "\n".join(lines))


# ------------------------------------------------------------ query engine
# On-demand corporate action queries across ALL NSE + BSE stocks (not just the
# watchlist). Every query fetches the live feed and filters it in memory, so
# the results are always fresh. Replies are split into Telegram-sized chunks.

MAX_QUERY_ITEMS = 20  # entries per message batch
MAX_NEWS_STOCKS = 10  # stocks processed by /news per request


def _split_messages(lines: list[str], max_len: int = 3800) -> list[str]:
    """Split text lines into messages under Telegram's 4096-char limit."""
    messages, current, size = [], [], 0
    for line in lines:
        line_len = len(line) + 1
        if current and size + line_len > max_len:
            messages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_len
    if current:
        messages.append("\n".join(current))
    return messages or [""]


def _reply_messages(
    chat_id, messages: list[str], reply_markup: dict | None = None
) -> None:
    for i, msg in enumerate(messages):
        try:
            # The keyboard rides on the first chunk; it persists in the chat
            # regardless of which message it is attached to.
            notifier.send_message(
                msg,
                chat_id=chat_id,
                reply_markup=reply_markup if i == 0 else None,
            )
        except notifier.NotifierError as exc:
            log.warning("query reply failed for chat %s: %s", chat_id, exc)
            return


def _attach_quotes(actions: list[dict], max_workers: int = 6) -> None:
    """Fetch current prices for the actions in parallel and attach them."""
    if not actions:
        return
    for a in actions:
        a.pop("quote", None)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(
            ex.map(
                lambda a: (a, sources.get_quote(a["exchange"], a["symbol"])),
                list(actions),
            )
        )
    attached = 0
    for action, quote in results:
        if quote:
            action["quote"] = quote
            attached += 1
    log.info(
        "attach_quotes: %d/%d quotes fetched", attached, len(actions)
    )


def _norm_type(token: str) -> str | None:
    """Normalize an action-type token (handles plurals like 'splits')."""
    t = token.strip().lower()
    if t in sources.ACTION_TYPES:
        return t
    if t.endswith("s") and t[:-1] in sources.ACTION_TYPES:
        return t[:-1]
    return None


def _parse_ca_arg(arg: str) -> dict | None:
    """Map one /ca argument to a query descriptor, or None when unclear."""
    raw = (arg or "").strip()
    token = raw.lower()
    if not raw:
        return None
    if token in ("increase", "shareholder", "shareholders", "shares", "share-holder"):
        return {"mode": "types", "types": list(sources.INCREASE_TYPES)}
    if (t := _norm_type(token)):
        return {"mode": "types", "types": [t]}
    if token in ("all", "list", "overview", "everything"):
        return {"mode": "overview"}
    if token in ("today", "tomorrow"):
        return {"mode": "exdate", "days": 0}
    try:
        return {"mode": "exdate", "days": max(0, int(token))}
    except ValueError:
        pass
    if "," in raw:  # e.g. /corpactions dividend,bonus
        wanted = [t for p in token.split(",") if (t := _norm_type(p))]
        if wanted:
            return {"mode": "types", "types": wanted}
    return {"mode": "term", "term": raw}


def _footnote(warnings: list, errors: list) -> list[str]:
    """BSE/network warnings shown as a note on overview queries."""
    notes = [n for n in (warnings or [])] + [n for n in (errors or [])]
    return [f"\u26a0\ufe0f {n}" for n in notes if n]


def run_ca_query(chat_id, descriptor: dict) -> bool:
    """Fetch all NSE+BSE actions, filter per descriptor, and reply."""
    log.info("ca_query: chat %s mode=%s", chat_id, descriptor.get("mode"))
    t0 = monotonic()
    try:
        all_actions, errors, warnings = poller_mod.fetch_all_actions()
    except Exception as exc:
        reply(chat_id, f"Could not fetch corporate actions: {config.redact(exc)}")
        return True
    log.info(
        "ca_query: fetched %d corporate action(s) in %.1fs (errors=%d, warnings=%d)",
        len(all_actions), monotonic() - t0, len(errors), len(warnings),
    )
    if not all_actions:
        note = "\n" + "\n".join(_footnote(warnings, errors)) if (warnings or errors) else ""
        reply(chat_id, "No corporate actions found right now." + note)
        return True

    mode = descriptor.get("mode")
    title = "<b>Corporate Actions</b> (all NSE + BSE)"
    results = None

    if mode == "overview":
        by_ex, by_type = {}, {}
        for a in all_actions:
            ex = a.get("exchange") or "?"
            by_ex[ex] = by_ex.get(ex, 0) + 1
            t = sources.action_type(a.get("subject"))
            by_type[t] = by_type.get(t, 0) + 1
        lines = [title]
        lines.append("Count by exchange: " + " | ".join(f"{k}: {v}" for k, v in by_ex.items()))
        type_summary = ", ".join(
            f"{sources.TYPE_LABELS.get(t, t)} {by_type.get(t, 0)}"
            for t in sources.ACTION_TYPES
            if by_type.get(t)
        )
        lines.append("Count by type: " + (type_summary or "none"))
        dated = sorted(
            (a for a in all_actions if poller_mod.parse_ex_date(a.get("ex_date"))),
            key=lambda a: a.get("ex_date"),
        )
        if dated:
            lines.append("\n<b>Next ex-dates:</b>")
            _attach_quotes(dated[:15])
            for a in dated[:15]:
                lines.append(notifier.format_action_entry(a))
        else:
            lines.append("\nNo ex-dates in the current feed.")
        messages = _split_messages(lines)
        if warnings or errors:
            messages.append("\n".join(_footnote(warnings, errors)))
        _reply_messages(chat_id, messages)
        return True

    if mode == "types":
        wanted = set(descriptor.get("types") or [])
        if len(wanted) == 1:
            label = sources.TYPE_LABELS.get(next(iter(wanted)), "Action")
        else:
            label = " + ".join(sources.TYPE_LABELS.get(t, t) for t in wanted)
        title = f"<b>{label} actions</b> (NSE + BSE)"
        results = [a for a in all_actions if sources.action_type(a.get("subject")) in wanted]

    elif mode == "exdate":
        days = int(descriptor.get("days", config.REMINDER_DAYS))
        today = config.today_ist()
        cutoff = today + timedelta(days=days)
        results = [
            a for a in all_actions
            if (d := poller_mod.parse_ex_date(a.get("ex_date"))) and today <= d <= cutoff
        ]
        label = "today" if days == 0 else f"within {days} day(s)"
        title = f"<b>Ex-date {label}</b> (NSE + BSE)"

    else:  # mode == "term": exact symbol first, then keyword search
        term = descriptor.get("term", "").strip()
        
        # Try to fetch symbol-specific actions from NSE to get full history
        symbol_matches = []
        try:
            from corp_actions import sources as sources_mod
            nse_actions = sources_mod.get_nse_corporate_actions(symbol=term.upper())
            for a in nse_actions:
                a["exchange"] = "NSE"
            symbol_matches.extend(nse_actions)
        except Exception as exc:
            log.info("Failed to fetch NSE corporate actions for %s: %s", term, exc)

        # Include matching symbols from the fetched global actions (e.g. BSE)
        for a in all_actions:
            if (a.get("symbol") or "").upper() == term.upper():
                # Avoid duplicates
                if not any(
                    sa.get("exchange") == a.get("exchange")
                    and sa.get("subject") == a.get("subject")
                    and sa.get("ex_date") == a.get("ex_date")
                    for sa in symbol_matches
                ):
                    symbol_matches.append(a)

        if symbol_matches:
            _attach_quotes(symbol_matches)
            messages = [f"<b>Corporate actions for {notifier.escape(term.upper())}</b>"]
            for a in sorted(
                symbol_matches, key=lambda x: x.get("ex_date") or "9999-99-99", reverse=True
            ):
                messages.append(notifier.format_action_detail(a))
            _reply_messages(chat_id, messages)
            return True
        q = term.lower()
        results = [
            a for a in all_actions
            if q in (a.get("company") or "").lower()
            or q in (a.get("subject") or "").lower()
        ]
        if not results:
            close = _close_symbols(term)
            if close:
                lines = [
                    f"No corporate actions match '{notifier.escape(term)}'. "
                    "Did you mean (NSE):"
                ]
                lines += [f"  /ca {c}" for c in close]
                reply(chat_id, "\n".join(lines))
                return True
        title = f"<b>Search results for '{notifier.escape(term)}'</b> (NSE + BSE)"

    if not results:
        reply(chat_id, f"No corporate actions match this query.\n\n{CA_HELP}")
        return True

    ordered = sorted(
        results,
        key=lambda a: (a.get("ex_date") or "9999-99-99", (a.get("symbol") or "").upper()),
    )
    shown = ordered[:MAX_QUERY_ITEMS]
    _attach_quotes(shown)
    lines = [title, f"{len(ordered)} action(s) found."]
    for a in shown:
        lines.append(notifier.format_action_entry(a))
    if len(ordered) > MAX_QUERY_ITEMS:
        lines.append(
            f"... and {len(ordered) - MAX_QUERY_ITEMS} more (limit "
            f"{MAX_QUERY_ITEMS}). Narrow it down with /corpactions dividend, "
            "/corpactions 7, or /corpactions SYMBOL."
        )
    _reply_messages(chat_id, _split_messages(lines))
    return True


QUICK_MENU_TEXT = (
    "<b>\U0001F447 One-Tap Command Menu</b>\n"
    "Tap any button below - the command runs instantly, no typing needed.\n\n"
    "\u2022 <code>/corpactionsformylist</code> - corporate actions for YOUR list\n"
    "\u2022 <code>/myfavourites</code> - all your regular commands in one go\n"
    "\u2022 <code>/corpactions</code> - all NSE+BSE corporate actions\n"
    "\u2022 <code>/corpactionssummary</code> - counts + next ex-dates\n"
    "\u2022 <code>/topgainers 1h</code> / <code>/toplosers 1h</code> - movers\n"
    "\u2022 <code>/watchlist</code> - your stocks\n\n"
    "The menu stays visible until you hide it with <code>/menu off</code>."
)


def handle_menu(chat_id, parts) -> None:
    """Show (or hide) the one-tap reply-keyboard menu (/menu, /quick)."""
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("off", "hide", "none", "remove"):
        reply(
            chat_id,
            "Quick menu hidden. Send <code>/menu</code> anytime to bring it back.",
            reply_markup=notifier.hide_keyboard_markup(),
        )
        return
    reply(chat_id, QUICK_MENU_TEXT, reply_markup=notifier.quick_menu_markup())


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
            f"Your list is saved in: {where}",
            "Customize with /alertfilters, /pricealert and /watcher.",
        ]
    )


def _parse_interval_min(raw: str) -> int | None:
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


def _next_at_ist(hhmm: str) -> float | None:
    """Epoch seconds of the next occurrence of an "HH:MM" wall-clock time in IST.

    Returns None when the string is not a valid HH:MM. Used by the schedule
    so a report can be tied to an exact clock time (e.g. run at 09:15 IST)
    instead of only an interval - and it lands on that minute regardless of
    the host's timezone.
    """
    import datetime as _dt
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = None
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    if tz is None:
        now_local = _dt.datetime.now()
        cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now_local:
            cand += _dt.timedelta(days=1)
        return cand.timestamp()
    now_ist = now_utc.astimezone(tz)
    cand = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now_ist:
        cand += _dt.timedelta(days=1)
    return cand.timestamp()


def _fmt_next_run(due_ts: float) -> str:
    """Human-friendly 'next run' for a schedule entry, e.g. 'in 35 min (14:20 IST)'."""
    import datetime as _dt
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
            line += f"  — next run {_fmt_next_run(due)}"
        lines.append(line)
    lines.append(
        "\nUsage: <code>/schedule add 3h /scan500</code> (interval: 180, 90m, 3h, 1d)"
    )
    lines.append("<code>/schedule remove 1</code>  /  <code>/schedule clear</code>")
    return "\n".join(lines)


def handle_sched(chat_id, parts) -> None:
    """Manage YOUR OWN automated-report schedule (works for every user).

    /schedule                  -> show YOUR schedule
    /schedule add <int> <cmd...> -> add a command on its own timer (e.g. /schedule add 3h /scan500)
    /schedule remove <n>       -> remove YOUR entry n (1-based, as shown by /schedule)
    /schedule clear            -> remove all of YOUR entries

    Everything is scoped to the requesting chat - one user's schedule can
    never change or disturb another user's.
    """
    sub = parts[1].lower() if len(parts) > 1 else ""
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
            if _next_at_ist(run_at) is None:
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
        interval = _parse_interval_min(interval_tok)
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

    reply(chat_id, format_schedule(chat_id))


def handle_news(chat_id, parts) -> None:
    """Latest news for the stocks in the requester's watchlist.

    /news           -> up to 3 headlines per watchlist stock
    /news N         -> up to N headlines per stock (1-5)
    /news SYMBOL    -> news for one symbol instead of the whole list
    """
    per_stock = 3
    if len(parts) > 1 and parts[1].isdigit():
        per_stock = max(1, min(5, int(parts[1])))
        target = None
    elif len(parts) > 1:
        target = parts[1].upper()
    else:
        target = None

    if target:
        items = [{"symbol": target, "exchange": "NSE", "company": target}]
    else:
        items = storage.get_user_list(chat_id)
        if not items:
            reply(
                chat_id,
                "Your watchlist is empty. Add stocks with /addstock SYMBOL NSE, "
                "then /news.",
            )
            return
    log.info(
        "news: chat %s target=%s per_stock=%d from %d stock(s)",
        chat_id, target or "watchlist", per_stock, len(items),
    )

    truncated = len(items) > MAX_NEWS_STOCKS
    items = items[:MAX_NEWS_STOCKS]

    def _fetch(item):
        try:
            news = sources.get_stock_news(item["exchange"], item["symbol"], per_stock)
            return item, news
        except Exception as exc:  # a failing stock must not break the batch
            log.info("news fetch failed for %s: %s", item.get("symbol"), exc)
            return item, []

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(_fetch, items))
    log.info("news: fetched headlines for %d/%d stock(s)", len(fetched), len(items))

    for item, news in fetched:
        try:
            notifier.send_message(
                notifier.format_news_list(item["symbol"], item["exchange"], news),
                chat_id=chat_id,
            )
        except notifier.NotifierError as exc:
            log.warning("news reply failed for chat %s: %s", chat_id, exc)
            return
    if truncated:
        reply(chat_id, f"(showing news for the first {MAX_NEWS_STOCKS} stocks)")


MOVERS_PERIODS = {
    # intraday: ("intraday", minutes)
    "5m": ("intraday", 5), "10m": ("intraday", 10), "15m": ("intraday", 15),
    "30m": ("intraday", 30), "45m": ("intraday", 45),
    "1h": ("intraday", 60), "2h": ("intraday", 120), "4h": ("intraday", 240),
    # multi-day: ("days", N)
    "today": ("days", 1), "day": ("days", 1), "1d": ("days", 1),
    "2d": ("days", 2), "3d": ("days", 3), "5d": ("days", 5), "7d": ("days", 7),
    "1w": ("days", 7), "week": ("days", 7), "2w": ("days", 14),
    "1mo": ("days", 30), "month": ("days", 30), "3mo": ("days", 90),
    "6mo": ("days", 180), "1y": ("days", 365), "year": ("days", 365),
}


def _period_label(kind: str, value: int) -> str:
    """Human label for a period, e.g. ('intraday', 60) -> 'last 1h'."""
    if kind == "intraday":
        if value % 60 == 0:
            return f"last {value // 60}h"
        return f"last {value}m"
    if value == 1:
        return "today"
    if value == 7:
        return "last 1 week"
    if value == 14:
        return "last 2 weeks"
    if value == 30:
        return "last 1 month"
    if value == 90:
        return "last 3 months"
    if value == 180:
        return "last 6 months"
    if value == 365:
        return "last 1 year"
    return f"last {value} days"


def _fetch_period_change(sym: str, period: tuple) -> dict | None:
    """Dispatch a (kind, value) period to the right Yahoo fetcher."""
    kind, value = period
    if kind == "intraday":
        return sources.get_intraday_change("NSE", sym, value)
    return sources.get_daily_change("NSE", sym, value)


def _parse_screen_parts(parts, default_period, default_direction,
                        default_count, default_universe) -> tuple:
    """Extract (period, direction, count, universe) from screen command args.

    One shared parser backs /movers, /gainers and /losers so they all accept
    the same tokens in any order:
      periods   5m 15m 30m 1h 2h 4h today 2d 3d 5d 1w 2w 1mo 3mo 6mo 1y
      direction gainers/losers/all
      count     any number 1-100
      universe  n100/nifty100 or n500/nifty500 keyword, or a second number

    A bare `100`/`500` means the index universe for /movers (which shows all
    stocks anyway) but a *count* for /gainers and /losers, so `/losers 1mo
    100` means "top 100 losers" while `/movers 500` means "NIFTY 500".
    """
    period, direction, count, universe = (
        default_period, default_direction, default_count, default_universe)
    explicit_count = False
    for token in parts[1:]:
        t = token.lower()
        if t in MOVERS_PERIODS:
            period = MOVERS_PERIODS[t]
        elif t in ("gainers", "gainer", "positive", "up"):
            direction = "gainers"
        elif t in ("losers", "loser", "negative", "down"):
            direction = "losers"
        elif t in ("all", "both", "mixed"):
            direction = "all"
        elif t in ("100", "n100", "nifty100", "nifty-100", "nifty 100"):
            if t == "100" and not explicit_count and default_count is not None:
                count = 100
                explicit_count = True
            else:
                universe = "nifty100"
        elif t in ("500", "n500", "allstocks", "all-stocks", "nifty500",
                   "nifty-500", "nifty 500"):
            if t == "500" and not explicit_count and default_count is not None:
                count = 500
                explicit_count = True
            else:
                universe = "nifty500"
        else:
            try:
                count = max(1, min(100, int(t)))
                explicit_count = True
            except ValueError:
                pass
    return period, direction, count, universe


def _format_price_movers_report(rows: list, header: str) -> str:
    """Format the fast initial price-only movers report (Phase 1)."""
    lines = [header]
    for idx, (sym, d) in enumerate(rows, 1):
        change = d["change_pct"]
        price = d.get("price")
        if change >= 3.0:
            move_icon = "\U0001F7E2\u25b2\u25b2"
        elif change >= 1.0:
            move_icon = "\U0001F7E2\u25b2"
        elif change <= -3.0:
            move_icon = "\U0001F534\u25bc\u25bc"
        elif change <= -1.0:
            move_icon = "\U0001F534\u25bc"
        elif change >= 0:
            move_icon = "\U0001F7E1\u25b2"
        else:
            move_icon = "\U0001F7E1\u25bc"
        sign = "+" if change >= 0 else ""
        lines.append(
            f"{idx}. {move_icon} <b>{notifier.escape(sym)}</b>  "
            f"{notifier.fmt_money(price)}  <b>{sign}{change:.2f}%</b>"
        )
    lines.append("")
    lines.append(
        f"\u23f3 Price data loaded for {len(rows)} stocks. "
        "Fetching 52W range, RSI, P/E &amp; fundamentals... "
        "Updated report coming in a few seconds."
    )
    return "\n".join(lines)


def _format_enriched_movers_report(rows: list, header: str, fund_by_sym: dict) -> str:
    """Format the full enriched fundamentals movers report with spacious card layout."""
    enriched_lines = [header, ""]
    for idx, (sym, d) in enumerate(rows, 1):
        change = d["change_pct"]
        price = d.get("price")
        fund = fund_by_sym.get(sym)
        if change >= 3.0:
            move_icon = "\U0001F7E2\u25b2\u25b2"
        elif change >= 1.0:
            move_icon = "\U0001F7E2\u25b2"
        elif change <= -3.0:
            move_icon = "\U0001F534\u25bc\u25bc"
        elif change <= -1.0:
            move_icon = "\U0001F534\u25bc"
        elif change >= 0:
            move_icon = "\U0001F7E1\u25b2"
        else:
            move_icon = "\U0001F7E1\u25bc"
        sign = "+" if change >= 0 else ""
        chg_str = f"{sign}{change:.2f}%"
        sig_emoji, _ = _wk52_signal(price, fund)
        sig_prefix = f" {sig_emoji}" if sig_emoji else ""
        enriched_lines.append(
            f"{idx}. {move_icon}{sig_prefix} <b>{notifier.escape(sym)}</b>  "
            f"{notifier.fmt_money(price)}  <b>{chg_str}</b>"
        )
        fund_lines = _fundamentals_lines(fund, price)
        for fl in fund_lines:
            enriched_lines.append("   " + fl)
        enriched_lines.append("")
    return "\n".join(enriched_lines)


def handle_market_screen(chat_id, parts, default_direction="all",
                         default_period=("intraday", 60), default_count=15,
                         default_universe="nifty100") -> None:
    """Screen an index universe by price movement over a time window.

    One implementation backs /movers, /gainers and /losers so all three stay
    feature-identical. Replies in two stages so the user never waits blind:
    an immediate acknowledgment, then the initial price-only report as soon
    as quotes are in, and finally an updated full report with fundamentals.
    """
    period, direction, count, universe = _parse_screen_parts(
        parts, default_period, default_direction, default_count,
        default_universe)

    universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
    period_label = _period_label(*period)
    t0 = monotonic()
    log.info(
        "screen %s: period=%s direction=%s count=%d universe=%s",
        parts[0], period_label, direction, count, universe_label,
    )

    # Phase 0 - acknowledge immediately so the user never waits blind while
    # the universe + quotes are fetched (NIFTY 500 can take a minute or two).
    reply(
        chat_id,
        f"Scanning {universe_label} over {period_label} for {direction}... "
        "This can take a minute or two.",
    )
    log.info("screen %s: sent initial acknowledgment", parts[0])

    symbols = sources.get_index_universe(universe)
    if not symbols:
        log.warning("screen %s: no symbols loaded for universe %s", parts[0], universe)
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return
    log.info(
        "screen %s: universe loaded (%d symbols) in %.1fs",
        parts[0], len(symbols), monotonic() - t0,
    )

    def _fetch(sym):
        return sym, _fetch_period_change(sym, period)

    fetched = []
    with ThreadPoolExecutor(max_workers=25) as ex:
        futures = {ex.submit(_fetch, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                data = fut.result()[1]
            except Exception as exc:
                data = None
                log.info(
                    "screen %s: change fetch failed for %s: %s",
                    parts[0], sym, exc,
                )
            fetched.append((sym, data))
            if done % 100 == 0 or done == len(symbols):
                log.info(
                    "screen %s: change fetch progress %d/%d symbols",
                    parts[0], done, len(symbols),
                )
    log.info(
        "screen %s: change fetch complete (%d symbols) in %.1fs",
        parts[0], len(symbols), monotonic() - t0,
    )

    rows = [(sym, d) for sym, d in fetched if d and d.get("change_pct") is not None]
    if direction == "gainers":
        rows = [r for r in rows if r[1]["change_pct"] > 0]
        rows.sort(key=lambda r: r[1]["change_pct"], reverse=True)  # highest first
        title = f"<b>Top Gainers - {period_label}</b>"
    elif direction == "losers":
        rows = [r for r in rows if r[1]["change_pct"] < 0]
        rows.sort(key=lambda r: r[1]["change_pct"])  # most negative first
        title = f"<b>Top Losers - {period_label}</b>"
    else:
        rows.sort(key=lambda r: r[1]["change_pct"])  # lower -> higher
        title = f"<b>Movers - {period_label}</b> · {direction} (lower \u2192 higher)"

    if count:
        rows = rows[:count]
    failed = len(fetched) - sum(
        1 for _, d in fetched if d and d.get("change_pct") is not None
    )
    if not rows:
        ok = len(fetched) - failed
        log.warning(
            "screen %s: no %s in %s over %s (universe=%d, quotes ok=%d/%d) - "
            "market may be closed or everything moved the other way",
            parts[0], direction, universe_label, period_label,
            len(symbols), ok, len(fetched),
        )
        reply(chat_id, f"No movement data found for {period_label} ({universe_label}).")
        return

    header = f"{title} · {universe_label} (Top {len(rows)})"

    # Phase 1 - the initial report: movers and their current price only, so
    # the user gets actionable numbers now instead of waiting for the slower
    # fundamentals enrichment.
    phase1_lines = _format_price_movers_report(rows, header)
    if failed:
        phase1_lines += f"\n({failed} of {len(symbols)} stocks could not be loaded)"
    _reply_messages(chat_id, _split_messages(phase1_lines.split("\n")))
    log.info(
        "screen %s: initial report sent (%d rows) in %.1fs",
        parts[0], len(rows), monotonic() - t0,
    )

    # Phase 2 - fetch fundamentals (Screener + Yahoo Finance) and send the
    # enriched report. To protect against screener.in's aggressive rate
    # limiting, the slow screener.in part is only fetched for the first
    # FUND_MAX_ROWS rows; the rest get the fast Yahoo-only fundamentals.
    def _fund_fetch(sym, with_screener):
        return sym, sources.get_fundamentals(sym, with_screener=with_screener)

    t_fund = monotonic()
    fund_by_sym = {}
    tasks = [
        (sym, i < sources.FUND_MAX_ROWS)
        for i, (sym, _) in enumerate(rows)
    ]
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {
            ex.submit(_fund_fetch, sym, with_screener): (sym, with_screener)
            for sym, with_screener in tasks
        }
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym, _ = futures[fut]
            try:
                fund_by_sym[sym] = fut.result()[1]
            except Exception as exc:  # fundamentals are best-effort
                fund_by_sym[sym] = None
                log.info(
                    "screen %s: fundamentals failed for %s: %s",
                    parts[0], sym, exc,
                )
            if done % 10 == 0 or done == len(tasks):
                log.info(
                    "screen %s: fundamentals progress %d/%d rows",
                    parts[0], done, len(tasks),
                )
    log.info(
        "screen %s: fundamentals fetch complete (%d rows) in %.1fs",
        parts[0], len(tasks), monotonic() - t_fund,
    )

    enriched_report = _format_enriched_movers_report(rows, header, fund_by_sym)
    if len(rows) > sources.FUND_MAX_ROWS:
        enriched_report += (
            f"\n(fundamentals detail shown for the first "
            f"{sources.FUND_MAX_ROWS} stocks)"
        )
    if failed:
        enriched_report += f"\n({failed} of {len(symbols)} stocks could not be loaded)"
    # Cross-link: one tappable button per top symbol -> deep fundamentals
    tap_symbols = [sym for sym, _ in rows[:10]]
    _reply_messages(
        chat_id,
        _split_messages(enriched_report.split("\n")),
        reply_markup=notifier.symbol_buttons(tap_symbols, "fund") if tap_symbols else None,
    )
    log.info(
        "screen %s: final report sent (%d rows) in %.1fs (total %.1fs), "
        "quote failures=%d",
        parts[0], len(rows), monotonic() - t_fund, monotonic() - t0, failed,
    )


def _wk52_signal(price, fund: dict | None) -> tuple:
    """Return (signal_emoji, range_tag) based on 52-week position of price."""
    if not fund:
        return "", ""
    lo = fund.get("wk52_low")
    hi = fund.get("wk52_high")
    if lo is None or hi is None or price is None:
        return "", ""
    try:
        price = float(price)
        lo = float(lo)
        hi = float(hi)
    except (TypeError, ValueError):
        return "", ""
    spread = hi - lo
    if spread <= 0:
        return "", ""
    pct_pos = (price - lo) / spread  # 0.0 = at 52W low, 1.0 = at 52W high
    if pct_pos <= 0.15:
        return "\u2705", "\U0001F7E2 Near 52W Low"
    if pct_pos <= 0.35:
        return "\U0001F4C8", "\U0001F7E2 Low Zone"
    if pct_pos >= 0.85:
        return "\U0001F6AB", "\U0001F534 Near 52W High"
    if pct_pos >= 0.65:
        return "\u26a0\ufe0f", "\U0001F534 High Zone"
    return "\U0001F7E1", "\U0001F7E1 Mid-Range"


def _rsi_signal(rsi: float | None) -> str:
    """Format 14-period RSI with clear signal emoji."""
    if rsi is None:
        return ""
    if rsi <= 30.0:
        return f"\U0001F7E2 RSI {rsi:g} (Oversold)"
    if rsi <= 45.0:
        return f"\U0001F7E2 RSI {rsi:g} (Low)"
    if rsi >= 70.0:
        return f"\U0001F534 RSI {rsi:g} (Overbought)"
    if rsi >= 60.0:
        return f"\U0001F534 RSI {rsi:g} (High)"
    return f"\U0001F7E1 RSI {rsi:g}"


def _fundamentals_lines(fund: dict | None, price=None) -> list[str]:
    """Format fundamentals as clean, spacious, structured lines."""
    if not fund:
        return []

    def _num(value, nd: int) -> str:
        s = f"{value:.{nd}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    sig_emoji, range_tag = _wk52_signal(price, fund)
    rsi_tag = _rsi_signal(fund.get("rsi"))

    lines = []

    # Line 1: Signals & Technicals
    l1_parts = []
    if range_tag:
        l1_parts.append(range_tag)
    if rsi_tag:
        l1_parts.append(rsi_tag)
    if l1_parts:
        lines.append("  \u2022  ".join(l1_parts))

    # Line 2: Valuation & Market Stats
    l2_parts = []
    if fund.get("pe"):
        l2_parts.append(f"P/E {_num(fund['pe'], 1)}")
    else:
        l2_parts.append("P/E N/A (Loss)")
    if fund.get("sector_pe"):
        l2_parts.append(f"Sec P/E {_num(fund['sector_pe'], 1)}")
    if fund.get("market_cap") is not None:
        l2_parts.append(f"MCap \u20b9{fund['market_cap']:,.0f}Cr")
    if fund.get("debt_to_equity") is not None:
        l2_parts.append(f"D/E {_num(fund['debt_to_equity'], 2)}")
    if l2_parts:
        lines.append("\U0001F4CA " + "  \u00b7  ".join(l2_parts))

    # Line 3: 52-Week Range & Returns
    l3_parts = []
    if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
        l3_parts.append(
            f"52w Range: {notifier.fmt_money(fund['wk52_low'])} \u2013 "
            f"{notifier.fmt_money(fund['wk52_high'])}"
        )
    if fund.get("div_yield") is not None:
        l3_parts.append(f"Div Yield: {_num(fund['div_yield'], 2)}%")
    if fund.get("roce") is not None or fund.get("roe") is not None:
        r_bits = []
        if fund.get("roce"):
            r_bits.append(f"ROCE {_num(fund['roce'], 1)}%")
        if fund.get("roe"):
            r_bits.append(f"ROE {_num(fund['roe'], 1)}%")
        l3_parts.append(" ".join(r_bits))
    if l3_parts:
        lines.append("\U0001F4C8 " + "  \u00b7  ".join(l3_parts))

    # Line 4: Shareholding Pattern (with QoQ trends!)
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        h_bits = []
        for key, label in (
            ("promoter_pct", "Prom"),
            ("fii_pct", "FII"),
            ("dii_pct", "DII"),
            ("public_pct", "Pub"),
        ):
            if fund.get(key):
                h_bits.append(f"{label} {notifier.escape(fund[key])}")
        lines.append("\U0001F4BC Holding (QoQ): " + "  \u00b7  ".join(h_bits))

    return lines


MAX_STOCK_BATCH = 10
MAX_FUND_BATCH = 5


def _parse_stock_range(arg: str):
    """Parse a watchlist position range like '5', '5-10', '5 - 10', 'all'.

    Returns a (start, end) tuple with 1-based inclusive positions (end None =
    to the end of the list), or None when the arg looks like a stock symbol.
    """
    token = (arg or "").strip().lower()
    if not token:
        return None
    if token in ("all", "mylist", "my-list", "my list", "*", "full", "everything"):
        return (1, None)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    m = re.fullmatch(r"(\d+)\s*-", token)
    if m:
        return (int(m.group(1)), None)
    m = re.fullmatch(r"-\s*(\d+)", token)
    if m:
        return (1, int(m.group(1)))
    if token.isdigit():
        return (1, max(1, int(token)))
    return None


def _stock_summary_lines(raw_sym, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the compact /stock summary card for one symbol."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    comp_name = quote.get("name") or raw_sym

    def _num(value, nd: int) -> str:
        try:
            s = f"{float(value):.{nd}f}"
        except (TypeError, ValueError):
            return "N/A"
        return s.rstrip("0").rstrip(".") if "." in s else s

    lines = []
    sec_name = notifier.escape(fund.get("sector") or "Indian Equity")
    lbl = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {lbl}<b>{notifier.escape(comp_name.upper())}</b> (<code>{notifier.escape(raw_sym)}</code>)"
    )
    lines.append(f"Sector: <i>{sec_name}</i>")
    lines.append("")

    # Section 1: Price & Today's Movement
    sig_emoji, range_tag = _wk52_signal(price, fund)
    rsi_tag = _rsi_signal(fund.get("rsi"))
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or range_tag or rsi_tag:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        if price is not None:
            p_str = notifier.fmt_money(price)
            if change_pct is not None:
                sign = "+" if change_pct >= 0 else ""
                abs_str = f" ({sign}{notifier.fmt_money(change_abs)})" if change_abs is not None else ""
                arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
                color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
                lines.append(
                    f"Current Price: <b>{p_str}</b>  {color_icon}{arrow} "
                    f"<b>{sign}{change_pct:.2f}%</b>{abs_str}"
                )
            else:
                lines.append(f"Current Price: <b>{p_str}</b>")

        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            hi, lo = fund["wk52_high"], fund["wk52_low"]
            lines.append(f"\U0001F4C8 52w Range: {notifier.fmt_money(lo)} \u2013 {notifier.fmt_money(hi)}")
            if price:
                try:
                    dist_lo = ((float(price) - lo) / lo) * 100
                    dist_hi = ((hi - float(price)) / hi) * 100
                    lines.append(f"📍 Distance: +{dist_lo:.1f}% from 52w Low  \u00b7  -{dist_hi:.1f}% from 52w High")
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

        if range_tag or rsi_tag:
            t_bits = [b for b in (range_tag, rsi_tag) if b]
            lines.append(f"\u26a1 Technicals: {'  •  '.join(t_bits)}")
        lines.append("")

    # Section 2: Valuation & Ratios
    lines.append("<b>\U0001F3F7\ufe0f VALUATION & RATIOS</b>")
    val_parts = []
    if fund.get("pe"):
        val_parts.append(f"Stock P/E: <b>{_num(fund['pe'], 1)}</b>")
    else:
        val_parts.append("Stock P/E: <b>N/A (Loss)</b>")

    if fund.get("sector_pe"):
        val_parts.append(f"Sec P/E: <b>{_num(fund['sector_pe'], 1)}</b>")

    if fund.get("market_cap") is not None:
        val_parts.append(f"MCap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")

    if fund.get("debt_to_equity") is not None:
        val_parts.append(f"D/E: <b>{_num(fund['debt_to_equity'], 2)}</b>")

    if fund.get("div_yield") is not None:
        val_parts.append(f"Div Yield: <b>{_num(fund['div_yield'], 2)}%</b>")

    lines.append("  \u00b7  ".join(val_parts))
    lines.append("")

    # Section 3: Profitability & Returns
    lines.append("<b>\U0001F3AF PROFITABILITY & RETURNS</b>")
    ret_parts = []
    if fund.get("roce") is not None:
        ret_parts.append(f"ROCE: <b>{_num(fund['roce'], 1)}%</b>")
    if fund.get("roe") is not None:
        ret_parts.append(f"ROE: <b>{_num(fund['roe'], 1)}%</b>")
    if ret_parts:
        lines.append("  \u00b7  ".join(ret_parts))
    else:
        lines.append("Full financial statement trends available on Screener.in")
    lines.append("")

    # Section 4: Shareholding Pattern (QoQ Trend)
    lines.append("<b>\U0001F4BC SHAREHOLDING PATTERN (QoQ TREND)</b>")
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        if fund.get("promoter_pct"):
            lines.append(f"\U0001F451 Promoter: {notifier.escape(fund['promoter_pct'])}")
        if fund.get("fii_pct"):
            lines.append(f"\U0001F30D FII: {notifier.escape(fund['fii_pct'])}")
        if fund.get("dii_pct"):
            lines.append(f"\U0001F3DB\ufe0f DII: {notifier.escape(fund['dii_pct'])}")
        if fund.get("public_pct"):
            lines.append(f"\U0001F465 Public: {notifier.escape(fund['public_pct'])}")
    else:
        lines.append("No shareholding breakdown available.")

    if include_tip:
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_sym} NSE</i>")
    return lines


def _fund_report_lines(raw_sym, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the deep /fund fundamental report for one symbol."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    comp_name = quote.get("name") or raw_sym

    def _num(value, nd: int = 1) -> str:
        if value is None:
            return "N/A"
        try:
            s = f"{float(value):.{nd}f}"
        except (TypeError, ValueError):
            return "N/A"
        return s.rstrip("0").rstrip(".") if "." in s else s

    def _pct(value) -> str:
        if value is None:
            return "N/A"
        try:
            return f"{float(value) * 100:+.1f}%"
        except (TypeError, ValueError):
            return "N/A"

    def _cr(value) -> str:
        if value is None:
            return "N/A"
        try:
            return f"\u20b9{float(value) / 1e7:,.1f}Cr"
        except (TypeError, ValueError):
            return "N/A"

    lines = []
    sec_name = notifier.escape(fund.get("sector") or "Indian Equity")
    ind_name = notifier.escape(fund.get("industry") or "")
    lbl = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {lbl}<b>{notifier.escape(comp_name.upper())}</b> (<code>{notifier.escape(raw_sym)}</code>)"
    )
    if ind_name:
        lines.append(f"Sector: <i>{sec_name}</i>  \u00b7  Industry: <i>{ind_name}</i>")
    else:
        lines.append(f"Sector: <i>{sec_name}</i>")
    lines.append("")

    # Section 1: Price & movement
    t_bits = [b for b in (_wk52_signal(price, fund)[1], _rsi_signal(fund.get("rsi"))) if b]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or t_bits:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        if price is not None:
            p_str = notifier.fmt_money(price)
            if change_pct is not None:
                sign = "+" if change_pct >= 0 else ""
                abs_str = f" ({sign}{notifier.fmt_money(change_abs)})" if change_abs is not None else ""
                arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
                color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
                lines.append(
                    f"Current Price: <b>{p_str}</b>  {color_icon}{arrow} "
                    f"<b>{sign}{change_pct:.2f}%</b>{abs_str}"
                )
            else:
                lines.append(f"Current Price: <b>{p_str}</b>")
        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            lines.append(
                f"\U0001F4C8 52w Range: {notifier.fmt_money(fund['wk52_low'])} \u2013 {notifier.fmt_money(fund['wk52_high'])}"
            )
        if t_bits:
            lines.append(f"\u26a1 Technicals: {'  •  '.join(t_bits)}")
        lines.append("")

    # Section 2: Valuation
    lines.append("<b>\U0001F3F7\ufe0f VALUATION</b>")
    val = []
    if fund.get("pe"):
        val.append(f"P/E: <b>{_num(fund['pe'])}</b>")
    else:
        val.append("P/E: <b>N/A (Loss)</b>")
    if fund.get("forward_pe"):
        val.append(f"Fwd P/E: <b>{_num(fund['forward_pe'])}</b>")
    if fund.get("sector_pe"):
        val.append(f"Sector P/E: <b>{_num(fund['sector_pe'])}</b>")
    if fund.get("price_to_book"):
        val.append(f"P/B: <b>{_num(fund['price_to_book'], 2)}</b>")
    if fund.get("price_to_sales"):
        val.append(f"P/S: <b>{_num(fund['price_to_sales'], 2)}</b>")
    if fund.get("div_yield") is not None:
        val.append(f"Div Yield: <b>{_num(fund['div_yield'], 2)}%</b>")
    if fund.get("beta") is not None:
        val.append(f"Beta: <b>{_num(fund['beta'], 2)}</b>")
    lines.append("  \u00b7  ".join(val))
    if fund.get("market_cap") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")
    elif fund.get("mcap_cr") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['mcap_cr']:,.0f}Cr</b>")
    if fund.get("enterprise_value") is not None:
        lines.append(f"Enterprise Value: <b>{_cr(fund['enterprise_value'])}</b>")
    lines.append("")

    # Section 3: Growth & margins (YoY)
    def _growth_pct(value) -> str:
        """YoY growth % with a green/red arrow so up/down reads at a glance."""
        s = _pct(value)
        try:
            v = float(value)
        except (TypeError, ValueError):
            return s
        arrow = "\U0001F7E2\u25b2" if v >= 0 else "\U0001F534\u25bc"
        return f"{arrow} {s}"

    grow = []
    if fund.get("earnings_growth") is not None:
        grow.append(f"Earnings: <b>{_growth_pct(fund['earnings_growth'])}</b>")
    if fund.get("revenue_growth") is not None:
        grow.append(f"Revenue: <b>{_growth_pct(fund['revenue_growth'])}</b>")
    marg = []
    if fund.get("gross_margin") is not None:
        marg.append(f"Gross: <b>{_pct(fund['gross_margin'])}</b>")
    if fund.get("ebitda_margin") is not None:
        marg.append(f"EBITDA: <b>{_pct(fund['ebitda_margin'])}</b>")
    if fund.get("operating_margin") is not None:
        marg.append(f"Operating: <b>{_pct(fund['operating_margin'])}</b>")
    if fund.get("profit_margin") is not None:
        marg.append(f"Net: <b>{_pct(fund['profit_margin'])}</b>")
    if grow or marg:
        lines.append("<b>\U0001F4C8 GROWTH & MARGINS (YoY)</b>")
        if grow:
            lines.append("  \u00b7  ".join(grow))
        if marg:
            lines.append("  \u00b7  ".join(marg))
        lines.append("")

    # Section 4: Per-share & scale
    per = []
    if fund.get("trailing_eps") is not None:
        per.append(f"EPS(TTM): <b>{_num(fund['trailing_eps'], 2)}</b>")
    if fund.get("forward_eps") is not None:
        per.append(f"EPS(Fwd): <b>{_num(fund['forward_eps'], 2)}</b>")
    if fund.get("revenue_per_share") is not None:
        per.append(f"Rev/Share: <b>{_num(fund['revenue_per_share'], 2)}</b>")
    if fund.get("book_value") is not None:
        per.append(f"Book Value: <b>{_num(fund['book_value'], 2)}</b>")
    if fund.get("cash_per_share") is not None:
        per.append(f"Cash/Share: <b>{_num(fund['cash_per_share'], 2)}</b>")
    if per or fund.get("shares_outstanding") is not None:
        lines.append("<b>\U0001F4BC PER-SHARE & SCALE</b>")
        if per:
            lines.append("  \u00b7  ".join(per))
        if fund.get("shares_outstanding") is not None:
            lines.append(f"Shares Outstanding: <b>{fund['shares_outstanding'] / 1e7:,.2f}Cr</b>")
        lines.append("")

    # Section 5: Balance sheet
    bs = []
    if fund.get("debt_to_equity") is not None:
        bs.append(f"D/E: <b>{_num(fund['debt_to_equity'], 2)}</b>")
    if fund.get("current_ratio") is not None:
        bs.append(f"Current Ratio: <b>{_num(fund['current_ratio'], 2)}</b>")
    if fund.get("quick_ratio") is not None:
        bs.append(f"Quick Ratio: <b>{_num(fund['quick_ratio'], 2)}</b>")
    if bs or fund.get("total_cash") is not None or fund.get("total_debt") is not None:
        lines.append("<b>\U0001F4C9 BALANCE SHEET</b>")
        if bs:
            lines.append("  \u00b7  ".join(bs))
        if fund.get("total_cash") is not None or fund.get("total_debt") is not None:
            lines.append(
                f"Cash: <b>{_cr(fund.get('total_cash'))}</b>  \u00b7  Debt: <b>{_cr(fund.get('total_debt'))}</b>"
            )
        cf = []
        if fund.get("free_cashflow") is not None:
            cf.append(f"Free Cash Flow: <b>{_cr(fund['free_cashflow'])}</b>")
        if fund.get("operating_cashflow") is not None:
            cf.append(f"Operating Cash Flow: <b>{_cr(fund['operating_cashflow'])}</b>")
        if cf:
            lines.append("  \u00b7  ".join(cf))
        lines.append("")

    # Section 6: Returns
    ret = []
    if fund.get("roce") is not None:
        ret.append(f"ROCE: <b>{_num(fund['roce'])}%</b>")
    if fund.get("roe") is not None:
        ret.append(f"ROE: <b>{_num(fund['roe'])}%</b>")
    if ret:
        lines.append("<b>\U0001F3AF RETURNS</b>")
        lines.append("  \u00b7  ".join(ret))
        lines.append("")

    # Section 7: Analyst view
    if fund.get("num_analysts") or fund.get("target_mean"):
        lines.append("<b>\U0001F52D ANALYST VIEW</b>")
        if fund.get("target_mean") is not None:
            tm = float(fund["target_mean"])
            ups = ""
            if price:
                pct = (tm - float(price)) / float(price) * 100
                if pct > 0:
                    ups = f"  (<b>+{pct:.0f}%</b> upside)"
                elif pct < 0:
                    ups = f"  (<b>{pct:.0f}%</b> downside)"
            lines.append(f"Target (Mean): <b>{notifier.fmt_money(tm)}</b>{ups}")
        hi_lo = []
        if fund.get("target_high") is not None:
            hi_lo.append(f"High {notifier.fmt_money(fund['target_high'])}")
        if fund.get("target_low") is not None:
            hi_lo.append(f"Low {notifier.fmt_money(fund['target_low'])}")
        if hi_lo:
            lines.append("  " + "  \u00b7  ".join(hi_lo))
        if fund.get("num_analysts"):
            lines.append(f"Analysts Covering: <b>{fund['num_analysts']}</b>")
        lines.append("")

    # Section 8: Shareholding QoQ trend
    lines.append("<b>\U0001F4BC SHAREHOLDING (QoQ TREND)</b>")
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        if fund.get("promoter_pct"):
            lines.append(f"\U0001F451 Promoter: {notifier.escape(fund['promoter_pct'])}")
        if fund.get("fii_pct"):
            lines.append(f"\U0001F30D FII: {notifier.escape(fund['fii_pct'])}")
        if fund.get("dii_pct"):
            lines.append(f"\U0001F3DB\ufe0f DII: {notifier.escape(fund['dii_pct'])}")
        if fund.get("public_pct"):
            lines.append(f"\U0001F465 Public: {notifier.escape(fund['public_pct'])}")
    else:
        lines.append("No shareholding breakdown available.")

    if include_tip:
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_sym} NSE</i>")
    return lines


def handle_single_stock_analysis(chat_id, parts) -> None:
    """Stock summary card for one symbol, or a watchlist position range.

    /stock TATATECH  → single symbol
    /stock 5         → first 5 watchlist stocks
    /stock 5-10      → watchlist positions 5..10
    /stock all       → whole watchlist
    """
    if len(parts) < 2:
        reply(
            chat_id,
            "Usage: <code>/fundamentalanalyze SYMBOL</code> (e.g. <code>/fundamentalanalyze TATATECH</code>) "
            "or <code>/fundamentalanalyze 5</code> / <code>/fundamentalanalyze 5-10</code> / "
            "<code>/fundamentalanalyze mylist</code> (watchlist positions)",
        )
        return

    rng = _parse_stock_range(parts[1])
    if rng is not None:
        handle_stock_batch(chat_id, "/fundamentalanalyze", rng, deep=False)
        return

    raw_sym = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    t0 = monotonic()
    log.info("handle_single_stock: fetching full details for %s for chat %s", raw_sym, chat_id)

    quote = sources.get_quote("NSE", raw_sym) or sources.get_quote("BSE", raw_sym) or {}
    fund = sources.get_fundamentals(raw_sym, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        _reply_suggestions(chat_id, raw_sym, "fundamentalanalyze")
        return

    lines = _stock_summary_lines(raw_sym, quote, fund, include_tip=True)
    _reply_messages(chat_id, ["\n".join(lines)])
    log.info("handle_single_stock: completed for %s in %.1fs", raw_sym, monotonic() - t0)


def _stock_next_markup(deep: bool, start: int) -> dict:
    """Inline 'Next' button for a paginated batch (callback_data stknext:deep:start)."""
    return {
        "inline_keyboard": [
            [{"text": "Next \u25b6", "callback_data": f"stknext:{1 if deep else 0}:{start}"}],
        ]
    }


def _build_stock_batch(chat_id, cmd: str, rng, deep: bool) -> tuple[list[str], int | None]:
    """Fetch and format a page of watchlist positions (never sends).

    Returns (lines, next_start) where next_start is the 1-based start of the
    next page, or None when this is the last page.
    """
    cap = MAX_FUND_BATCH if deep else MAX_STOCK_BATCH
    items = storage.get_user_list(chat_id)
    if not items:
        return ["Your watchlist is empty. Add stocks with /addstock SYMBOL NSE"], None
    start, end = rng
    total = len(items)
    start = max(1, start)
    end = total if end is None else min(end, total)
    if start > total:
        return [
            f"Your watchlist has only {total} stock(s) - start position must be 1..{total}."
        ], None
    work = items[start - 1:end][:cap]
    t0 = monotonic()
    log.info(
        "%s batch: positions %d-%d (%d stocks, deep=%s)",
        cmd, start, start + len(work) - 1, len(work), deep,
    )

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(work)))) as ex:
        quotes = list(ex.map(lambda it: sources.get_quote(it["exchange"], it["symbol"]) or {}, work))
        funds = list(ex.map(lambda it: sources.get_fundamentals(it["symbol"], with_screener=True) or {}, work))

    body = []
    for pos, (item, quote, fund) in enumerate(zip(work, quotes, funds), start=start):
        sym = item["symbol"]
        label = f"<b>#{pos}</b>"
        if deep:
            lines = _fund_report_lines(sym, quote, fund, include_tip=False, label=label)
        else:
            lines = _stock_summary_lines(sym, quote, fund, include_tip=False, label=label)
        if not quote and not fund:
            lines = [
                f"\U0001F4CA {label} <b>{notifier.escape(sym)}</b>",
                "\u26a0\ufe0f No data available right now.",
            ]
        body.extend(lines)
        body.append("")

    header = (
        f"\U0001F4CA <b>{cmd.upper()} \u00b7 Watchlist positions "
        f"{start}\u2013{start + len(work) - 1} of {total}</b>\n"
    )
    all_lines = [header] + body
    next_start = start + len(work) if start + len(work) <= total else None
    if next_start is not None:
        remaining = total - (start + len(work) - 1)
        page_end = min(total, next_start + cap - 1)
        all_lines.append(
            f"\u2026 and {remaining} more. Tap <b>Next \u25b6</b> below or send "
            f"<code>{cmd} {next_start}-{page_end}</code> for the next batch."
        )
    log.info("%s batch: done %d stocks in %.1fs", cmd, len(work), monotonic() - t0)
    return all_lines, next_start


def handle_stock_batch(chat_id, cmd: str, rng, deep: bool) -> None:
    """Render /fundamentalanalyze or /fundamentalreport for a range of watchlist positions."""
    lines, next_start = _build_stock_batch(chat_id, cmd, rng, deep)
    markup = _stock_next_markup(deep, next_start) if next_start else None
    _reply_messages(chat_id, _split_messages(lines), reply_markup=markup)


def _answer_callback(callback_id) -> None:
    """Acknowledge an inline-button tap so Telegram clears the loading spinner."""
    if not callback_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=config.HTTP_TIMEOUT,
        )
    except Exception as exc:
        log.info("answerCallbackQuery failed: %s", config.redact(exc))


def handle_callback_query(callback) -> None:
    """Handle an inline-button tap.

    Supported buttons:
      stknext:<deep>:<start> - the 'Next' pagination button on stock batches
      fund:<SYMBOL>          - symbol button -> /fundamentalreport SYMBOL
      ana:<SYMBOL>           - symbol button -> /fundamentalanalyze SYMBOL

    Answers the callback first so Telegram clears the loading spinner.
    """
    data = (callback.get("data") or "").strip()
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    callback_id = callback.get("id")
    if not data or chat_id is None:
        return
    _answer_callback(callback_id)

    if data.startswith("fund:") or data.startswith("ana:"):
        # Symbol cross-link buttons: tap a ticker in any report to open its
        # fundamentals immediately (deep /fundamentalreport or the quick card).
        sym = data.split(":", 1)[1].strip().upper()
        if not sym:
            return
        log.info("callback %s for symbol %s (chat %s)", data.split(":", 1)[0], sym, chat_id)
        if data.startswith("fund:"):
            handle_fund_analysis(chat_id, ["/fundamentalreport", sym])
        else:
            handle_single_stock_analysis(chat_id, ["/fundamentalanalyze", sym])
        return

    if not data.startswith("stknext:"):
        return
    parts = data.split(":")
    if len(parts) != 3:
        return
    _, deep_s, start_s = parts
    try:
        deep = bool(int(deep_s))
        start = int(start_s)
    except ValueError:
        return
    cmd = "/fundamentalreport" if deep else "/fundamentalanalyze"
    lines, next_start = _build_stock_batch(chat_id, cmd, (start, None), deep)
    markup = _stock_next_markup(deep, next_start) if next_start else None
    _reply_messages(chat_id, _split_messages(lines), reply_markup=markup)


def handle_fund_analysis(chat_id, parts) -> None:
    """Deep fundamental report for one symbol, or a watchlist position range.

    /fundamentalreport RELIANCE  → single symbol
    /fundamentalreport 5         → first 5 watchlist stocks
    /fundamentalreport 5-10      → watchlist positions 5..10
    /fundamentalreport mylist    → whole watchlist
    """
    if len(parts) < 2:
        reply(
            chat_id,
            "Usage: <code>/fundamentalreport SYMBOL</code> (e.g. <code>/fundamentalreport RELIANCE</code>) "
            "or <code>/fundamentalreport 5</code> / <code>/fundamentalreport 5-10</code> / "
            "<code>/fundamentalreport mylist</code> (watchlist positions)",
        )
        return

    rng = _parse_stock_range(parts[1])
    if rng is not None:
        handle_stock_batch(chat_id, "/fundamentalreport", rng, deep=True)
        return

    raw_sym = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    t0 = monotonic()
    log.info("handle_fund: deep fundamentals for %s (chat %s)", raw_sym, chat_id)

    quote = sources.get_quote("NSE", raw_sym) or sources.get_quote("BSE", raw_sym) or {}
    fund = sources.get_fundamentals(raw_sym, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        _reply_suggestions(chat_id, raw_sym, "fundamentalreport")
        return

    lines = _fund_report_lines(raw_sym, quote, fund, include_tip=True)
    _reply_messages(chat_id, _split_messages(lines))
    log.info("handle_fund: completed for %s in %.1fs", raw_sym, monotonic() - t0)


HARMONIC_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d", "1w")

# Universe keywords for the bulk screener. "all"/bare defaults to NIFTY 100,
# "500" switches to NIFTY 500 (mirrors the movers 100|500 index selector).
HARMONIC_SCAN_UNIVERSES = {
    "all": "nifty100",
    "100": "nifty100",
    "nifty100": "nifty100",
    "nifty-100": "nifty100",
    "500": "nifty500",
    "nifty500": "nifty500",
    "nifty-500": "nifty500",
}


def handle_harmonic(chat_id, parts) -> None:
    """Harmonic pattern scanner.

    Screener mode (compact, one line per stock - a "smaller version" of the
    full report so the whole index fits in a few messages):
      /harmonic          -> NIFTY 100, daily
      /harmonic all      -> NIFTY 100, daily
      /harmonic 500      -> NIFTY 500, daily
      /harmonic 500 1w   -> NIFTY 500, weekly chart
    Single-stock detail (full report with PRZ, entry, SL & targets):
      /harmonic SYMBOL [TIMEFRAME]   e.g. /harmonic RELIANCE, /harmonic TATATECH 1h
      /harmonic 3                    -> full report for watchlist #3
    """
    if len(parts) >= 2 and parts[1].lower() in HARMONIC_SCAN_UNIVERSES:
        universe = HARMONIC_SCAN_UNIVERSES[parts[1].lower()]
        tf = "1d"
        if len(parts) >= 3:
            cand = parts[2].lower()
            if cand in HARMONIC_TIMEFRAMES:
                tf = cand
            else:
                reply(
                    chat_id,
                    f"Unknown timeframe <code>{html.escape(parts[2])}</code>. "
                    f"Options: {', '.join(HARMONIC_TIMEFRAMES)}",
                )
                return
        handle_harmonic_scan(chat_id, universe, tf)
        return

    if len(parts) < 2:
        # Bare /harmonic -> default NIFTY 100 screener, like /movers.
        handle_harmonic_scan(chat_id, "nifty100", "1d")
        return

    raw = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    tf = "1d"
    if len(parts) >= 3:
        cand = parts[2].lower()
        if cand in HARMONIC_TIMEFRAMES:
            tf = cand
        else:
            reply(
                chat_id,
                f"Unknown timeframe <code>{html.escape(parts[2])}</code>. "
                f"Options: {', '.join(HARMONIC_TIMEFRAMES)}",
            )
            return

    if raw.isdigit():
        items = storage.get_user_list(chat_id)
        n = int(raw)
        if not items:
            reply(chat_id, "Your watchlist is empty — add stocks with <code>/add SYMBOL</code> first.")
            return
        if n < 1 or n > len(items):
            reply(chat_id, f"Your watchlist has {len(items)} stock(s) — use a position 1..{len(items)}.")
            return
        item = items[n - 1]
        symbol, exchange = item["symbol"], item["exchange"]
    else:
        symbol, exchange = raw, "NSE"

    t0 = monotonic()
    log.info("handle_harmonic: scanning %s on %s (chat %s)", symbol, tf, chat_id)
    try:
        res = harmonic.analyze(exchange, symbol, tf)
    except Exception as exc:
        log.warning("handle_harmonic failed for %s: %s", symbol, exc)
        reply(chat_id, f"Could not scan <code>{html.escape(symbol)}</code>: {html.escape(str(exc))}")
        return
    if not res:
        _reply_suggestions(chat_id, symbol, "harmonic")
        return

    lines = harmonic.format_report(res)
    _reply_messages(chat_id, _split_messages(lines))
    log.info("handle_harmonic: done %s %s in %.1fs", symbol, tf, monotonic() - t0)


def handle_harmonic_scan(chat_id, universe, tf) -> None:
    """Bulk /harmonic screener over an index universe (compact report).

    Scans every symbol in NIFTY 100 / NIFTY 500 for a harmonic formation and
    replies with one compact line per stock that has one, sorted most
    actionable first. Full detail for any stock is one /harmonic SYMBOL away.
    """
    universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
    t0 = monotonic()
    log.info(
        "harmonic scan: universe=%s timeframe=%s (chat %s)",
        universe_label, tf, chat_id,
    )
    reply(
        chat_id,
        f"Scanning {universe_label} stocks for harmonic patterns on the "
        f"{tf} chart... this can take a minute or two.",
    )

    symbols = sources.get_index_universe(universe)
    if not symbols:
        log.warning("harmonic scan: no symbols loaded for %s", universe)
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return
    log.info("harmonic scan: universe loaded (%d symbols)", len(symbols))

    def _scan(sym):
        try:
            return sym, harmonic.analyze("NSE", sym, tf, light=True)
        except Exception as exc:  # one bad symbol must not kill the scan
            log.info("harmonic scan: failed for %s: %s", sym, exc)
            return sym, None

    found = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(_scan, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                res = fut.result()[1]
            except Exception as exc:
                res = None
            if res and res.get("pattern") and res.get("status") not in (
                "No harmonic pattern detected", "Pattern invalidated",
            ):
                found.append(res)
            if done % 50 == 0 or done == len(symbols):
                log.info(
                    "harmonic scan: progress %d/%d symbols, %d pattern(s)",
                    done, len(symbols), len(found),
                )

    log.info(
        "harmonic scan: %d/%d symbols with a pattern in %.1fs",
        len(found), len(symbols), monotonic() - t0,
    )
    if not found:
        reply(
            chat_id,
            f"No harmonic patterns detected across {universe_label} on the {tf} chart.",
        )
        return

    found.sort(
        key=lambda r: (
            harmonic.SCAN_PRIORITY.get(r.get("status"), 9),
            r.get("pattern") or "",
            r["symbol"],
        )
    )
    shown = found[:harmonic.SCAN_MAX_ROWS]
    lines = [
        f"<b>HARMONIC SCAN - {universe_label}</b> \u00b7 {tf} chart",
        f"{len(found)} stock(s) showing a formation"
        + (f" (top {len(shown)} by actionability)" if len(found) > len(shown) else ""),
    ]
    for idx, r in enumerate(shown, 1):
        lines.append(f"{idx}. {harmonic.format_scan_row(r)}")
    lines.append("")
    lines.append("Use /harmonic SYMBOL for the full report (PRZ, entry, SL & targets).")
    _reply_messages(chat_id, _split_messages(lines))
    log.info(
        "harmonic scan: sent %d row(s) in %.1fs",
        len(shown), monotonic() - t0,
    )


def handle_scan500(chat_id, parts) -> None:
    """NIFTY 500 advanced multi-indicator CNC/MIS scanner (/scan500).

    Runs the full indicator suite (EMAs, RSI, MACD, ADX, CMF, MFI, OBV,
    Aroon, TTM squeeze, Donchian, weekly Supertrend, GMMA, anchored VWAP,
    Mansfield RS) over the NIFTY 500 universe, applies the strict rejection
    rules, scores survivors out of 100 and reports regime + top picks.
    """
    t0 = monotonic()
    log.info("scan500: starting (chat %s)", chat_id)
    reply(
        chat_id,
        "Scanning NIFTY 500 (daily candles, ~500 stocks)... "
        "this can take a minute or two.",
    )
    try:
        import corp_actions.scanner as sc
    except Exception as exc:
        log.warning("scan500: scanner module unavailable: %s", exc)
        reply(chat_id, "Scanner engine unavailable (pandas missing?).")
        return

    symbols = sources.get_index_universe("nifty500")
    if not symbols:
        log.warning("scan500: no symbols loaded")
        reply(chat_id, "Could not load the NIFTY 500 universe right now. Try again in a minute.")
        return

    # Market regime inputs
    idx50 = sources.get_index_ohlc("^NSEI", "2y", "1d")
    vix = sources.get_index_ohlc("^INDIAVIX", "6mo", "1d")
    idx500 = sources.get_index_ohlc("^CRSLDX", "2y", "1d") or idx50
    bench_close = (idx500 or {}).get("close")

    def _fetch(sym):
        try:
            return sym, sources.get_ohlc("NSE", sym, "1d")
        except Exception:
            return sym, None

    ohlc_by_sym = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                ohlc_by_sym[sym] = fut.result()[1]
            except Exception:
                ohlc_by_sym[sym] = None
    log.info("scan500: fetched %d/%d OHLC sets in %.1fs",
             sum(1 for v in ohlc_by_sym.values() if v), len(symbols),
             monotonic() - t0)

    rows = []
    rejected = []
    for sym, ohlc in ohlc_by_sym.items():
        try:
            f = sc.scan_stock(ohlc, index_close=bench_close)
            if f is None:
                continue
            f = sc.build_plan(f)
            score, breakdown = sc.score_stock(f)
            reasons = sc.rejection_reasons(f)
            f["score"] = score
            if reasons:
                rejected.append((sym, f.get("name") or sym, f["price"], reasons))
            else:
                rows.append({"fields": f, "score": score, "breakdown": breakdown})
        except Exception as exc:
            log.info("scan500: skip %s (%s)", sym, exc)
            continue

    # Breadth across the scanned universe
    above_50 = sum(1 for _, o in ohlc_by_sym.items() if _above_ema(o, 50))
    above_200 = sum(1 for _, o in ohlc_by_sym.items() if _above_ema(o, 200))
    adv = sum(1 for s, o in ohlc_by_sym.items() if o and o["close"] and o["close"][-1] > o["open"][-1])
    dec = sum(1 for s, o in ohlc_by_sym.items() if o and o["close"] and o["close"][-1] < o["open"][-1])
    total = sum(1 for o in ohlc_by_sym.values() if o)
    vix_val = (vix or {}).get("close")
    vix_last = vix_val[-1] if vix_val else None
    breadth = {
        "above_ema50": (above_50 / total * 100.0) if total else None,
        "above_ema200": (above_200 / total * 100.0) if total else None,
        "advance": float(adv), "decline": float(dec),
        "vix": vix_last,
    }
    regime = sc.market_regime(idx50, breadth)

    # Approve by score threshold
    rows.sort(key=lambda r: r["score"], reverse=True)
    approved = [r for r in rows if r["score"] >= sc.SCORE_QUALIFY]

    lines = sc.format_report({
        "regime": regime,
        "rejected": rejected,
        "approved": approved,
        "scanned": total or len(symbols),
    })
    _reply_messages(chat_id, _split_messages(lines))
    log.info("scan500: done in %.1fs (%d approved, %d rejected, %d scanned)",
             monotonic() - t0, len(approved), len(rejected), total or len(symbols))


def _above_ema(ohlc, span: int) -> bool:
    """Quick price-vs-EMA check for breadth (no pandas dependency here)."""
    if not ohlc or len(ohlc["close"]) < span + 5:
        return False
    closes = ohlc["close"]
    price = closes[-1]
    k = 2.0 / (span + 1.0)
    ema = closes[-span]
    for c in closes[-span:]:
        ema = c * k + ema * (1 - k)
    return price > ema


def handle_movers(chat_id, parts) -> None:
    """Movement screen over an index (default NIFTY 100, all directions)."""
    handle_market_screen(
        chat_id, parts,
        default_direction="all",
        default_period=("intraday", 60),
        default_count=None,
        default_universe="nifty100",
    )


def handle_gainers_losers(chat_id, parts, direction: str) -> None:
    """Top gainers / losers over an index (default NIFTY 500, top 30, today)."""
    handle_market_screen(
        chat_id, parts,
        default_direction=direction,
        default_period=("days", 1),
        default_count=30,
        default_universe="nifty500",
    )


def handle_query_text(chat_id, text) -> bool:
    """Answer natural-language questions about corporate actions.

    Returns True when the message matched a known query pattern (a reply was
    sent). Deliberately conservative so random chat messages are ignored.
    """
    if not config.NATURAL_QUERIES:
        return False
    low = (text or "").strip().lower()
    if not low:
        return False
    keywords = (
        "corporate action", "corporate-action",
        "ex-date", "ex date",
        "dividend", "bonus", "split", "rights", "buyback", "buy back",
        "shareholder increase", "share holder increase", "shares increase",
        "increase", "actions", "news", "movers", "movement", "gainers", "losers",
    )
    if not any(k in low for k in keywords):
        return False
    if "gainers" in low or "gainer" in low:
        handle_gainers_losers(chat_id, ["/gainers"], "gainers")
        return True
    if "losers" in low or "loser" in low:
        handle_gainers_losers(chat_id, ["/losers"], "losers")
        return True
    if any(w in low for w in ("movers", "movement", "stock movement", "market movement")):
        handle_movers(chat_id, ["/movers"])
        return True
    if "news" in low and any(
        w in low for w in ("stock", "latest", "market", "watchlist", "list",
                           "hold", "holding", "share", "company")
    ):
        handle_news(chat_id, ["/news"])
        return True
    if "increase" in low and ("share" in low or "holder" in low):
        descriptor = {"mode": "types", "types": list(sources.INCREASE_TYPES)}
    elif "ex-date" in low or "ex date" in low or "upcoming" in low:
        descriptor = {"mode": "exdate", "days": config.REMINDER_DAYS}
    elif "dividend" in low:
        descriptor = {"mode": "types", "types": ["dividend"]}
    elif "bonus" in low:
        descriptor = {"mode": "types", "types": ["bonus"]}
    elif "split" in low:
        descriptor = {"mode": "types", "types": ["split"]}
    elif "rights" in low:
        descriptor = {"mode": "types", "types": ["rights"]}
    elif "buyback" in low or "buy back" in low:
        descriptor = {"mode": "types", "types": ["buyback"]}
    elif "corporate action" in low or "actions" in low or low.strip().startswith("ca "):
        descriptor = {"mode": "overview"}
    else:
        return False
    run_ca_query(chat_id, descriptor)
    return True


def register_commands() -> bool:
    """Publish the bot's command menu via Telegram setMyCommands."""
    if not notifier.is_configured():
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
        {"command": "menu", "description": "One-tap command buttons - no typing"},
        {"command": "help", "description": "Show all commands and examples"},
    ]
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        resp = requests.post(url, json={"commands": menu}, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        ok = bool(resp.json().get("ok"))
        log.info("setMyCommands %s", "ok" if ok else "failed")
        return ok
    except Exception as exc:
        log.warning("setMyCommands failed: %s", config.redact(exc))
        return False


def process_commands():
    """Process any pending Telegram command updates.

    Returns the chat_id that requested /checknow, or None.
    """
    if not config.PROCESS_COMMANDS:
        log.info("PROCESS_COMMANDS=false - skipping Telegram command processing")
        return None
    if not notifier.is_configured():
        return None
    try:
        updates = get_updates()
    except Exception as exc:  # broad on purpose: never let a getUpdates hiccup kill the run
        log.warning("getUpdates failed: %s", config.redact(exc), exc_info=True)
        return None

    checknow_chat = None
    max_offset = 0
    for update in updates:
        update_id = update.get("update_id", 0)
        max_offset = max(max_offset, update_id)
        callback = update.get("callback_query")
        if callback:
            try:
                handle_callback_query(callback)
            except Exception as exc:  # one bad tap must not break the run
                log.warning("callback query failed: %s", config.redact(exc))
            continue
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if not text.startswith("/"):
            try:
                handle_query_text(chat_id, text)
            except Exception as exc:  # a bad query must never break the loop
                log.warning("natural query failed: %s", config.redact(exc))
            continue
        if text.strip().lower() == "/checknow":
            checknow_chat = str(chat_id)
        try:
            handle_command(chat_id, text)
        except Exception as exc:  # one bad command must not kill the cron run
            log.warning("command failed for chat %s: %s", chat_id, config.redact(exc), exc_info=True)
    if max_offset:
        # Mark updates as consumed.
        get_updates(offset=max_offset + 1)
    return checknow_chat


def _schedule_entries_with_defaults(default_chat: str) -> list[dict]:
    """schedule.json entries plus the owner's env-default report.

    The env defaults (SCHEDULED_COMMANDS) keep running for the owner chat
    until the owner adds their own file entries. Subscribers adding their
    own entries must never suppress those defaults - this guarantees one
    user's schedule never disturbs another's.
    """
    entries = storage.load_schedule()
    cmds = [c for c in config.SCHEDULED_COMMANDS if c.strip()]
    if cmds:
        owner_has_entries = any(
            str(e.get("chat") or default_chat) == str(default_chat)
            for e in entries
        )
        if not owner_has_entries:
            entries = entries + [{
                "interval_min": config.SCHEDULED_REPORTS_INTERVAL_MIN,
                "commands": cmds,
                "chat": default_chat,
            }]
    return entries


def start_scheduled_reports():
    """Run scheduled reports to EACH user's own chat on a timer (daemon thread).

    Only the always-on server runs these (PROCESS_COMMANDS=true); the GitHub
    Actions cron and any other process skip them so scans are never sent
    twice. The first report fires a short while after startup so the server
    has finished booting before the scans hit the data feeds.

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
        return _schedule_entries_with_defaults(default_chat)

    # (chat, commands) -> monotonic() seconds when the next run is due.
    # Keyed on the entry's identity (not its list index) so one user adding
    # or removing their entries never changes another user's timing.
    next_due = {}

    def _loop():
        import time as _time
        while True:
            try:
                now = monotonic()
                entries = _entries()
                if not entries:
                    log.info("scheduled reports: schedule is empty - nothing to run")
                    next_due.clear()
                    _time.sleep(60)
                    continue
                # Drop due-times for entries that no longer exist so removed
                # schedules never linger, and (chat, commands) stays stable
                # across add/remove in other users' rows.
                alive = {
                    (str(e.get("chat") or default_chat),
                     tuple(c for c in e.get("commands") or [] if c.strip()))
                    for e in entries
                }
                next_due = {k: v for k, v in next_due.items() if k in alive}
                for entry in entries:
                    interval = int(entry.get("interval_min") or config.SCHEDULED_REPORTS_INTERVAL_MIN)
                    commands = [c for c in entry.get("commands") or [] if c.strip()]
                    chat = str(entry.get("chat") or default_chat)
                    if not commands:
                        continue
                    key = (chat, tuple(commands))
                    # Persisted next-due (schedule.json) wins so the cadence
                    # survives redeploys. For a clock-time entry (run_at) the
                    # first due is the next occurrence of HH:MM in IST. Plain
                    # interval entries fall back to a short first-run delay.
                    persisted = storage.schedule_next_due_ts(entry)
                    due = next_due.get(key)
                    if due is None:
                        if persisted is not None:
                            due = persisted
                        elif entry.get("run_at"):
                            due = _next_at_ist(entry["run_at"])
                            if due is None:
                                due = now + min(interval * 60, 60)
                        else:
                            due = now + min(interval * 60, 60)
                        next_due[key] = due
                    if now < due:
                        continue
                    for cmd in commands:
                        try:
                            log.info("scheduled report: running %s (chat %s)", cmd, chat)
                            handle_command(chat, cmd)
                        except Exception as exc:  # one bad report must not stop the loop
                            log.warning(
                                "scheduled report %s failed: %s",
                                cmd, config.redact(exc), exc_info=True,
                            )
                    # Schedule the next run AND persist it, so a redeploy
                    # resumes the same cadence instead of restarting the clock.
                    nxt = _time.time() + interval * 60
                    next_due[key] = nxt
                    storage.set_schedule_next_due(chat, commands, interval, nxt)
            except Exception as exc:  # never let a scheduler hiccup kill the thread
                log.warning(
                    "scheduled reports loop error: %s",
                    config.redact(exc), exc_info=True,
                )
            _time.sleep(30)

    threading.Thread(target=_loop, daemon=True, name="scheduled-reports").start()


# ----------------------------------------------------------------------- git
def _remote_default_branch(remote_url) -> str:
    try:
        out = _git("git", "ls-remote", "--symref", remote_url, "HEAD").stdout
        for line in out.splitlines():
            if line.strip().startswith("ref:"):
                # Line looks like:  ref: refs/heads/main\tHEAD
                # The ref path is the SECOND token; the trailing "HEAD" is
                # only the name of the ref being described. Taking the LAST
                # token here returns "HEAD", which turns the push refspec
                # into HEAD:HEAD and fails every push from a detached
                # checkout (e.g. Render) with "You must fully qualify the
                # ref" - the exact bug that made stocks vanish on redeploy.
                return line.split()[1].removeprefix("refs/heads/")
    except Exception:
        pass
    if remote_url:
        log.warning(
            "Could not determine the remote default branch (git ls-remote "
            "failed) - state will be pushed to 'main'. If the repo's "
            "default branch is not 'main', set GH_PUSH_BRANCH to override."
        )
    return ""


def _push_branch(remote_url: str) -> str:
    """Resolve the branch that state is pushed to / synced from."""
    branch = os.getenv("GH_PUSH_BRANCH") or ""
    if not branch:
        branch = _git("git", "symbolic-ref", "--short", "HEAD").stdout.strip()
    if not branch:
        branch = _remote_default_branch(remote_url)
    if branch == "HEAD":
        # "HEAD" can never be a real branch name - it means resolution
        # leaked/parsed incorrectly. Falling back to main keeps the push
        # refspec valid instead of pushing HEAD:HEAD and failing.
        log.warning("Push branch resolved as 'HEAD' - falling back to main")
        branch = "main"
    return branch or "main"


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            list(args), 124, stdout="", stderr=f"command timed out after {timeout}s"
        )


# The four state files that must reach GitHub to survive a redeploy.
STATE_FILES = (
    config.WATCHLIST_FILE,
    config.SUBSCRIPTIONS_FILE,
    config.SETTINGS_FILE,
    config.SEEN_FILE,
    config.SCHEDULE_FILE,
)


def pending_state_changes() -> str:
    """Comma-separated names of state files with uncommitted changes.

    Empty string means the worktree is clean. Used by /status and by the
    always-on server's periodic flush to decide whether a push is needed.
    """
    res = _git(
        "git", "status", "--porcelain", "--untracked-files=no",
        *[str(f) for f in STATE_FILES],
    )
    if res.returncode != 0:
        return ""
    names = []
    for line in res.stdout.splitlines():
        path = line[3:].strip().strip('"')
        names.append(Path(path).name)
    return ", ".join(sorted(set(names)))


def _ahead_of_origin(branch: str) -> bool:
    """True when the local branch has commits not present on origin/{branch}.

    This is the signal that a previous commit was never pushed - pushing
    again is required; a hard reset at this point would destroy data.
    """
    res = _git("git", "rev-list", "--count", f"origin/{branch}..HEAD")
    if res.returncode != 0:
        return False
    try:
        return int(res.stdout.strip()) > 0
    except ValueError:
        return False


# Reason for the last push_state() failure ("" when OK). bot_server reads this
# so the "NOT pushed to GitHub" warning can say WHY instead of guessing.
push_error = ""


def _redact_gh(text) -> str:
    """Mask the GH_TOKEN from git output before it reaches logs or Telegram.

    A failed push echoes the remote URL - including the embedded
    x-access-token - back on stderr. Without masking, the token would leak
    into server logs and into the /status "last error" reply.
    """
    s = str(text)
    token = os.getenv("GH_TOKEN")
    if token:
        s = s.replace(token, "***")
    return s


def push_state() -> bool:
    """Commit and push watchlist/seen state back to the repo, if changed.

    Returns True when the repo is in sync (pushed, or nothing to push).
    Returns False when credentials are missing or the push failed - callers
    should NOT discard local state in that case.

    Handles the expected race with the hourly cron (both push to the same
    branch): on a rejected push it fetches, rebases onto the remote and
    retries once.

    On failure, sets the module-global `push_error` to a short reason.
    """
    global push_error
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        push_error = "GH_TOKEN / GITHUB_REPOSITORY not set on this host"
        log.warning(
            "GH_TOKEN/GITHUB_REPOSITORY not set - skipping push. State is "
            "only on this host's disk and WILL BE LOST on redeploy. Set "
            "GH_TOKEN (fine-grained PAT, Contents: Read and write) and "
            "GITHUB_REPOSITORY (e.g. RaviRoyalTest/stockTelegramBot) in the "
            "host environment."
        )
        return False
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    branch = _push_branch(remote_url)

    _git("git", "config", "user.email", "actions@github.com")
    _git("git", "config", "user.name", "github-actions")
    # Only stage state files that actually exist on disk. A brand-new state
    # file (e.g. schedule.json before the first /sched write) is not in a
    # fresh checkout; "git add" with a nonexistent pathspec aborts with
    # "pathspec did not match any files" and would fail the ENTIRE push,
    # leaving every state change stranded on the ephemeral disk.
    existing = [str(f) for f in STATE_FILES if f.exists()]
    missing = [f.name for f in STATE_FILES if not f.exists()]
    if missing:
        log.info(
            "Skipping git add for missing state file(s): %s",
            ", ".join(sorted(missing)),
        )
    if not existing:
        log.info("No state files on disk - nothing to stage")
        staged = ""
    else:
        added = _git("git", "add", *existing)
        if added.returncode != 0:
            push_error = "git add failed: " + (_redact_gh(added.stderr.strip()[-200:]) or "unknown error")
            log.warning(
                "git add failed - state NOT pushed (local changes kept): %s",
                _redact_gh(added.stderr.strip()[-300:]),
            )
            return False
        staged = _git("git", "diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        # Nothing staged. But there may be local commits from a previous run
        # that failed to push. If we are ahead of origin, retry the push
        # instead of claiming "in sync" - otherwise a later sync_state()'s
        # reset --hard would silently destroy those commits.
        if _ahead_of_origin(branch):
            push = _git("git", "push", remote_url, f"HEAD:{branch}")
            if push.returncode == 0:
                log.info("Pushed previously-unpushed state to %s", branch)
                push_error = ""
                return True
            push_error = "git push failed: " + (_redact_gh(push.stderr.strip()[-200:]) or "unknown error")
            log.warning(
                "Retry push of existing local commits failed: %s",
                _redact_gh(push.stderr.strip()[-300:]),
            )
            return False
        log.info("No state change to push")
        push_error = ""
        return True
    log.info(
        "Staged state files: %s", ", ".join(staged.splitlines())
    )

    commit = _git("git", "commit", "-m", "chore: update watchlist from Telegram")
    if commit.returncode != 0:
        # Keep the changes in the worktree instead of the index so a later
        # sync (reset --hard) refuses to wipe them.
        push_error = "git commit failed: " + (_redact_gh(commit.stderr.strip()[-200:]) or "unknown error")
        log.warning("State commit failed: %s", _redact_gh(commit.stderr.strip()[-300:]))
        _git("git", "reset")
        return False

    push = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push.returncode == 0:
        log.info("Pushed state to %s", branch)
        push_error = ""
        return True

    # Expected race with the cron: retry once after rebasing onto remote.
    _git("git", "fetch", "origin")
    rebase = _git("git", "rebase", f"origin/{branch}")
    if rebase.returncode != 0:
        _git("git", "rebase", "--abort")
        push_error = (
            "git push failed after rebase conflict: "
            + (_redact_gh(push.stderr.strip()[-200:]) or "unknown error")
        )
        log.warning(
            "Push failed and rebase aborted (conflict): %s",
            _redact_gh(push.stderr.strip()[-300:]),
        )
        return False
    push2 = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push2.returncode == 0:
        log.info("Pushed state to %s (after rebase)", branch)
        push_error = ""
        return True
    push_error = (
        "git push failed after rebase: "
        + (_redact_gh(push2.stderr.strip()[-200:]) or "unknown error")
    )
    log.warning("Push failed after rebase: %s", _redact_gh(push2.stderr.strip()[-500:]))
    return False


def sync_state() -> bool:
    """Pull the latest committed state from GitHub before handling commands.

    GitHub is the source of truth; an always-on server's local copy is just a
    working checkout whose disk is ephemeral. Sync before serving commands so
    the server never answers with stale data or overwrites newer state.
    Never resets when the working tree has uncommitted changes (a failed push
    from a previous run) - that would wipe data.
    Returns True when synced or skipped safely (no credentials / dirty tree).
    """
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("GH_TOKEN/GITHUB_REPOSITORY not set - skipping state sync")
        return True
    try:
        remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        branch = _push_branch(remote_url)
        dirty = _git("git", "status", "--porcelain").stdout.strip()
        if dirty:
            log.warning(
                "State sync skipped: uncommitted changes present - push them "
                "first (dirty: %s)",
                dirty[:200],
            )
            return True
        _git("git", "fetch", "origin")
        if _ahead_of_origin(branch):
            # Local commits exist that were never pushed. A hard reset here
            # would silently destroy them - push them first instead.
            log.warning(
                "State sync skipped: local branch is ahead of origin/%s "
                "(unpushed commits). Run push_state or fix credentials first.",
                branch,
            )
            return True
        res = _git("git", "reset", "--hard", f"origin/{branch}")
        if res.returncode == 0:
            log.info("State synced from origin/%s", branch)
            return True
        log.warning("State sync failed: %s", _redact_gh(res.stderr.strip()[-300:]))
        return False
    except Exception as exc:
        log.warning("State sync failed: %s", _redact_gh(exc))
        return False


# ------------------------------------------------------------------- diag
def main_check() -> int:
    """Diagnostic for the 'my changes vanish on redeploy' problem.

    Prints whether the GitHub push is configured, whether the token can
    actually read/write the repo, which branch state is pushed to, and
    whether any state is currently unsaved. Exit code 0 = persistence OK.

    Run on the host itself (e.g. Render's Shell tab):
        python run_bot.py --check
    """
    print("=" * 62)
    print("Persistence diagnostic - will /add survive a redeploy?")
    print("=" * 62)

    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    ok = bool(token and repo)

    print("\n[1] Environment")
    print(
        "  GH_TOKEN            : "
        + (f"SET ({token[:4]}...)" if token else "NOT SET")
    )
    print(f"  GITHUB_REPOSITORY   : {repo or 'NOT SET'}")

    sha = _git("git", "rev-parse", "--short", "HEAD").stdout.strip() or "unknown"
    sym = _git("git", "symbolic-ref", "--short", "HEAD").stdout.strip()
    branch = _push_branch("")
    detached = not sym
    print("\n[2] Git")
    print(
        f"  HEAD                : {sha} "
        f"({'detached HEAD' if detached else 'on branch ' + sym})"
    )
    print(f"  Push/sync branch    : {branch}")
    for f in STATE_FILES:
        tracked = _git("git", "ls-files", "--error-unmatch", str(f)).returncode == 0
        status = "tracked" if tracked else "NOT tracked - push_state cannot save it"
        print(f"  {f.name:<22}: {status}")
        if not tracked:
            ok = False
    pending = pending_state_changes()
    print(f"  Uncommitted state   : {pending or 'none'}")

    if token and repo:
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
        print(f"\n[3] GitHub access via GH_TOKEN (repo: {repo})")
        ls = _git("git", "ls-remote", url, "HEAD")
        if ls.returncode == 0:
            print("  read  (ls-remote)    : OK")
        else:
            print(f"  read  (ls-remote)    : FAILED - {_redact_gh(ls.stderr.strip()[-200:])}")
            ok = False
        dry = _git("git", "push", "--dry-run", url, "HEAD:refs/heads/__state_check__")
        if dry.returncode == 0:
            print(
                "  write (push dry-run) : OK - a push would be accepted "
                "(no branch created)"
            )
        else:
            print(f"  write (push dry-run) : FAILED - {_redact_gh(dry.stderr.strip()[-300:])}")
            ok = False
    else:
        print("\n[3] GitHub access: skipped (set GH_TOKEN and GITHUB_REPOSITORY first)")

    print("\n[4] Verdict")
    if ok:
        print(f"  OK - state is pushed to GitHub ({repo}, branch {branch}) and")
        print("  WILL survive redeploys. Confirm in Telegram with /status.")
    else:
        print("  NOT OK - changes saved here will be LOST on redeploy.")
        print("  Fix the items above, then re-run:  python run_bot.py --check")
        print("  (On Render: set GH_TOKEN + GITHUB_REPOSITORY in the service's")
        print("   environment, redeploy, and run this again from the Shell tab.)")
    print()
    return 0 if ok else 1


# ------------------------------------------------------------------------- main
def main():
    if any(a.lower() == "--check" for a in sys.argv[1:]):
        sys.exit(main_check())
    log.info("Processing Telegram commands...")
    register_commands()
    checknow_chat = process_commands()
    log.info("Running poll cycle%s...", f" (forced for {checknow_chat})" if checknow_chat else "")
    sent = poller.run_once(force=bool(checknow_chat), only_chat=checknow_chat)
    log.info("Pushing state if changed...")
    push_state()
    log.info("Done. Sent %s alert(s).", sent)


if __name__ == "__main__":
    main()
