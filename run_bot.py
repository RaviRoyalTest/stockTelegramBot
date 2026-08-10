"""Entry point for running in a GitHub Actions cron job.

Two jobs, one run:
  1. Optionally process Telegram bot commands (/add, /remove, /list, /help)
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
    "\U0001F50D <b>Single Stock Deep Analysis</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/stock <i>SYMBOL</i>  \u2014 quick summary card\n"
    "  /stock TATATECH  \u2192 price, P/E, 52W signal, QoQ shareholding\n"
    "/stock <i>N | N-M | all</i>  \u2014 watchlist positions\n"
    "  /stock 5         \u2192 first 5 watchlist stocks\n"
    "  /stock 5-10      \u2192 watchlist stocks #5 to #10\n"
    "/fund <i>SYMBOL</i>  \u2014 deep fundamental report\n"
    "  /fund RELIANCE   \u2192 valuation, growth, margins, balance sheet,\n"
    "                     EPS, analyst targets &amp; shareholding\n"
    "  /fund 3-5        \u2192 deep report for watchlist #3..#5\n\n"
    "\U0001F3C6 <b>Harmonic Patterns &amp; PRZ</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/harmonic <i>[all|100|500] [TIMEFRAME]</i>\n"
    "  Scans an index for Gartley / Bat / Butterfly / Crab / Shark setups and\n"
    "  lists every stock showing a formation (compact, one line per stock).\n"
    "  /harmonic all          \u2192 NIFTY 100, daily\n"
    "  /harmonic 500          \u2192 NIFTY 500, daily\n"
    "  /harmonic 500 1w       \u2192 NIFTY 500, weekly chart\n"
    "  /harmonic SYMBOL [TIMEFRAME]  \u2192 full report with PRZ, entry, SL &amp; targets\n"
    "  /harmonic RELIANCE     \u2192 daily scan  \u00b7  /harmonic TATATECH 1h\n"
    "  /harmonic 3            \u2192 full report for watchlist #3\n"
    "  Timeframes: 5m 15m 30m 1h 4h 1d 1w\n\n"
    "\U0001F4CA <b>NIFTY 500 CNC/MIS Scanner</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/scan500\n"
    "  Advanced multi-indicator scan of the full NIFTY 500 universe:\n"
    "  EMAs, RSI, MACD, ADX, CMF, MFI, OBV, Aroon, TTM Squeeze, Donchian,\n"
    "  weekly Supertrend, GMMA, Anchored VWAP &amp; Mansfield RS.\n"
    "  Applies strict \u201cdo not buy / do not show\u201d rejection rules, scores\n"
    "  survivors /100 (\u226575 qualifies), picks the #1 setup and maps an\n"
    "  hour-by-hour CNC vs MIS execution plan.\n"
    "  /scan500        \u2192 full NIFTY 500 scan (takes ~1-2 min)\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4C8 <b>Market Screens</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/movers <i>[period] [N] [100|500]</i>\n"
    "  Top movers (up &amp; down) in a time window.\n"
    "  /movers          \u2192 last 1h, NIFTY 100\n"
    "  /movers 30m      \u2192 last 30 minutes\n"
    "  /movers 2d 500   \u2192 2-day movers, NIFTY 500\n"
    "  /movers 1w 10    \u2192 top 10 movers this week\n\n"
    "/gainers <i>[period] [N] [100|500]</i>\n"
    "  Top rising stocks. Default: today, NIFTY 500, top 30.\n"
    "  /gainers             \u2192 today's top gainers\n"
    "  /gainers 1h          \u2192 last 1h gainers\n"
    "  /gainers 1mo 20 500  \u2192 top 20 gainers this month, NIFTY 500\n"
    "  /gainers 3mo nifty100\u2192 3-month gainers, NIFTY 100\n\n"
    "/losers <i>[period] [N] [100|500]</i>\n"
    "  Top falling stocks. Default: today, NIFTY 500, top 30.\n"
    "  /losers             \u2192 today's top losers\n"
    "  /losers 1h 10       \u2192 top 10 losers last hour\n"
    "  /losers 1mo 500     \u2192 biggest losers this month, NIFTY 500\n"
    "  /losers 1w nifty100 \u2192 weekly losers, NIFTY 100\n\n"
    "Periods: 5m \u00b7 15m \u00b7 30m \u00b7 1h \u00b7 2h \u00b7 4h \u00b7 1d \u00b7 2d \u00b7 5d \u00b7 1w \u00b7 2w \u00b7 1mo \u00b7 3mo \u00b7 6mo \u00b7 1y\n"
    "Universe: n100/nifty100=NIFTY 100 \u00b7 n500/nifty500=NIFTY 500\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4C5 <b>Corporate Actions (NSE + BSE)</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/ca                  \u2192 overview of all upcoming actions\n"
    "/ca dividend         \u2192 dividend announcements\n"
    "/ca bonus            \u2192 bonus share issues\n"
    "/ca split            \u2192 stock splits\n"
    "/ca rights           \u2192 rights issues\n"
    "/ca buyback          \u2192 buybacks\n"
    "/ca increase         \u2192 bonus + split + rights combined\n"
    "/ca today            \u2192 ex-dates due today\n"
    "/ca 7                \u2192 ex-dates within next 7 days\n"
    "/ca RELIANCE         \u2192 full details for one symbol\n"
    "/ca TATA             \u2192 keyword search (company/subject)\n\n"
    "/exdate [today|N]    \u2192 all actions by ex-date window (default 5 days)\n"
    "  /exdate today      \u2192 ex-dates today\n"
    "  /exdate 10         \u2192 ex-dates in next 10 days\n\n"
    "/summary             \u2192 counts by type + next ex-dates\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\u2B50 <b>Watchlist</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/add SYMBOL [NSE|BSE]\u2192 add a stock (default NSE)\n"
    "  /add RELIANCE NSE  \u00b7  /add PGINVIT\n"
    "/remove SYMBOL       \u2192 remove from watchlist\n"
    "  /remove TCS\n"
    "/list                \u2192 view your full watchlist\n"
    "/next                \u2192 ex-dates + in-progress actions (rights/dividends)\n"
    "                       for your watchlist (upcoming + last 30 days)\n"
    "/news [N|SYMBOL]     \u2192 latest headlines\n"
    "  /news              \u2192 news for all watchlist stocks\n"
    "  /news 5            \u2192 5 headlines per stock\n"
    "  /news RELIANCE     \u2192 news for RELIANCE only\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\u2699\ufe0f <b>Personalise</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/filter TYPE,TYPE    \u2192 receive only selected action types\n"
    "  /filter dividend,bonus  \u00b7  /filter all (reset)\n"
    "  Types: dividend \u00b7 bonus \u00b7 split \u00b7 rights \u00b7 buyback\n"
    "/alert PCT           \u2192 alert when stock moves \u00b1PCT% in a day\n"
    "  /alert 3           \u2192 alert on \u00b13% move\n"
    "  /alert 1.5         \u00b7  /alert off (disable)\n"
    "/settings            \u2192 view your current filter &amp; alert config\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F6E0 <b>System</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/status              \u2192 where your watchlist is saved &amp; GitHub push status\n"
    "/sched add 3h /scan500 \u2192 run /scan500 automatically every 3h\n"
    "/checknow            \u2192 force-run alerts and re-send all matches\n"
    "/help \u00b7 /start       \u2192 show this guide\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4A1 <b>Quick Examples</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/gainers 1h 10          \u2192 Top 10 gainers last hour\n"
    "/losers 1mo 500         \u2192 Monthly losers \u2014 NIFTY 500\n"
    "/movers 2d 10 500       \u2192 2-day movers, top 10, NIFTY 500\n"
    "/scan500               \u2192 full NIFTY 500 CNC/MIS technical scan\n"
    "/ca dividend            \u2192 Upcoming dividends\n"
    "/ca RELIANCE            \u2192 RELIANCE corporate actions\n"
    "/add INFY NSE           \u2192 Add INFY to watchlist\n"
    "/news RELIANCE          \u2192 Latest RELIANCE headlines\n"
    "/filter bonus,split     \u2192 Only bonus &amp; split alerts\n"
    "/alert 2.5              \u2192 Alert on \u00b12.5% daily move\n"
    "\U0001F4DD Type in plain text: \"gainers\", \"dividends\", \"ex-date today\", \"news\""
)

CA_HELP = (
    "Corporate Action queries (/ca):\n"
    "/ca - overview of all NSE + BSE actions\n"
    "/ca dividend | bonus | split | rights | buyback - one action type\n"
    "/ca increase - shareholder increase (bonus + split + rights)\n"
    "/ca today - ex-date today\n"
    "/ca 7 - ex-date within 7 days\n"
    "/ca RELIANCE - details for one symbol\n"
    "/ca TATA - keyword search in company name / subject"
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


def reply(chat_id, text, parse_mode="HTML"):
    """Send a message to a chat, splitting into chunks if text exceeds Telegram limits."""
    if len(text) > 3800:
        msgs = _split_messages(text.split("\n"))
        _reply_messages(chat_id, msgs)
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
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


def send_help(chat_id):
    """Send the styled HTML help message (/help, /start, unknown commands)."""
    _reply_messages(chat_id, _split_messages(HELP_TEXT.split("\n")))


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

    if cmd == "/list":
        items = storage.get_user_list(chat_id)
        if not items:
            reply(chat_id, "Your watchlist is empty.")
        else:
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
            reply(
                chat_id,
                "<b>Your Watchlist:</b>\n"
                + "\n".join(lines)
                + f"\n\nUse <code>/stock 5-10</code> or <code>/fund 3-5</code> to get details by these numbers."
                + f"\nSaved in: <code>{html.escape(where)}</code>\nPersistence: {html.escape(persistence)}",
            )
        return

    if cmd == "/checknow":
        reply(chat_id, "Running a forced check now - re-sending all matching alerts shortly.")
        return

    if cmd == "/next":
        items = storage.get_user_list(chat_id)
        if not items:
            reply(chat_id, "Your watchlist is empty.")
            return
        try:
            matching = poller_mod.fetch_matching(items)
        except Exception as exc:
            reply(chat_id, f"Could not fetch corporate actions: {html.escape(str(exc))}")
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
        reply(chat_id, notifier.format_next_report(upcoming, recent, pending))
        return

    if cmd == "/filter":
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

    if cmd == "/alert":
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

    if cmd == "/status":
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
        owner_chat = config.TELEGRAM_CHAT_ID or "NOT SET"
        owner_line = (
            f"<b>Configured owner chat:</b> <code>{html.escape(owner_chat)}</code>"
            + ("" if owner else " - you are NOT this chat, so owner commands "
               "(/sched, watchlist.json) are unavailable to you")
        )
        reply(
            chat_id,
            "\n".join(
                [
                    f"<b>Your chat id:</b> <code>{chat_id}</code>",
                    f"<b>Role:</b> {'owner' if owner else 'subscriber'}",
                    owner_line,
                    f"<b>Saved in:</b> <code>{html.escape(location)}</code>",
                    f"<b>GitHub push:</b> {html.escape(push_status)}",
                    html.escape(sync_line),
                    f"<b>Scheduled reports:</b> "
                    + ("enabled" if config.SCHEDULED_REPORTS_ENABLED and config.PROCESS_COMMANDS else "off")
                    + " \u00b7 " + html.escape(format_schedule(chat_id).split("\n")[0])
                    + f" \u00b7 manage with /sched",
                    "Run /list to see your current watchlist.",
                ]
            ),
        )
        return

    if cmd in ("/ca", "/corpactions", "/actions", "/shareholder", "/increase"):
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

    if cmd == "/exdate":
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

    if cmd == "/summary":
        run_ca_query(chat_id, {"mode": "overview"})
        return

    if cmd == "/settings":
        reply(chat_id, format_settings(chat_id))
        return

    if cmd == "/sched":
        handle_sched(chat_id, parts)
        return

    if cmd == "/news":
        handle_news(chat_id, parts)
        return

    if cmd == "/movers":
        handle_movers(chat_id, parts)
        return

    if cmd in ("/gainers", "/losers"):
        direction = "gainers" if cmd == "/gainers" else "losers"
        handle_gainers_losers(chat_id, parts, direction)
        return

    if cmd == "/fund":
        handle_fund_analysis(chat_id, parts)
        return

    if cmd == "/harmonic":
        handle_harmonic(chat_id, parts)
        return

    if cmd == "/scan500":
        handle_scan500(chat_id, parts)
        return

    if cmd in ("/stock", "/info", "/quote"):
        handle_single_stock_analysis(chat_id, parts)
        return

    if len(parts) < 2:
        if cmd in ("/add", "/remove"):
            reply(chat_id, "Usage: <code>/add SYMBOL [NSE|BSE]</code> or <code>/remove SYMBOL [NSE|BSE]</code>")
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

    if cmd == "/add":
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
    elif cmd == "/remove":
        storage.remove_from_user_list(chat_id, symbol, exchange)
        log.info("Removed %s (%s) for chat %s", symbol, exchange, chat_id)
        reply(chat_id, f"Removed <b>{symbol}</b> ({exchange}) if it was present.")
    else:
        send_help(chat_id)


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
            lines.append(f"  /add {m['symbol']} NSE  - {notifier.escape(company)}")
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


def _reply_messages(chat_id, messages: list[str]) -> None:
    for msg in messages:
        try:
            notifier.send_message(msg, chat_id=chat_id)
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
    if "," in raw:  # e.g. /ca dividend,bonus
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
            f"{MAX_QUERY_ITEMS}). Narrow it down with /ca dividend, /ca 7, "
            "or /ca SYMBOL."
        )
    _reply_messages(chat_id, _split_messages(lines))
    return True


def format_settings(chat_id) -> str:
    """Render the per-chat customization settings (/settings)."""
    settings = storage.get_user_settings(chat_id)
    filters = settings.get("action_filters") or []
    alert = settings.get("price_alert_pct")
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
            f"Your list is saved in: {where}",
            "Customize with /filter and /alert.",
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


def format_schedule(chat_id) -> str:
    """Render the current automated-report schedule (/sched)."""
    entries = storage.load_schedule()
    if not entries:
        cmds = [c for c in config.SCHEDULED_COMMANDS if c.strip()]
        if not cmds:
            return "<b>Schedule:</b> no automated reports."
        lines = [
            "<b>Schedule (env defaults - use /sched to edit)</b>",
            f"  1. every {config.SCHEDULED_REPORTS_INTERVAL_MIN} min: "
            + html.escape(", ".join(cmds)),
        ]
    else:
        lines = ["<b>Schedule (schedule.json - pushed to GitHub)</b>"]
        for i, e in enumerate(entries, start=1):
            interval = int(e.get("interval_min") or 0)
            cmds = e.get("commands") or []
            chat = e.get("chat")
            label = f"every {interval} min"
            if interval and interval % (24 * 60) == 0:
                label = f"every {interval // (24 * 60)}d"
            elif interval and interval % 60 == 0:
                label = f"every {interval // 60}h"
            target = f" to {chat}" if chat and str(chat) != str(config.TELEGRAM_CHAT_ID) else ""
            lines.append(
                f"  {i}. {label}: {html.escape(', '.join(cmds))}{html.escape(target)}"
            )
    lines.append(
        "\nUsage: <code>/sched add 3h /scan500</code> (interval: 180, 90m, 3h, 1d)"
    )
    lines.append("<code>/sched remove 1</code>  /  <code>/sched clear</code>")
    return "\n".join(lines)


def handle_sched(chat_id, parts) -> None:
    """Manage the automated-report schedule (owner only).

    /sched                     -> show the current schedule
    /sched add <int> <cmd...>  -> add a command on its own timer (e.g. /sched add 3h /scan500)
    /sched remove <n>          -> remove entry n (1-based, as shown by /sched)
    /sched clear               -> remove all entries
    """
    if not storage.is_owner(chat_id):
        if not config.TELEGRAM_CHAT_ID:
            reply(
                chat_id,
                "Only the owner can change the schedule, and this host has "
                "no TELEGRAM_CHAT_ID configured, so nobody is recognized as "
                "owner. Set TELEGRAM_CHAT_ID on the server (your chat id is "
                f"<code>{chat_id}</code>), then try again.",
            )
        else:
            reply(
                chat_id,
                "Only the owner can change the schedule. Your chat id "
                f"(<code>{chat_id}</code>) is not the configured owner chat "
                f"(<code>{html.escape(config.TELEGRAM_CHAT_ID)}</code>). "
                "Use /status to compare.",
            )
        return

    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "add":
        if len(parts) < 4:
            reply(
                chat_id,
                "Usage: <code>/sched add &lt;interval&gt; &lt;command&gt;</code>\n"
                "e.g. <code>/sched add 3h /scan500</code> or "
                "<code>/sched add 90m /movers 30m</code>\n"
                "Interval: minutes (180), m (90m), h (3h) or d (1d), min 15.",
            )
            return
        interval = _parse_interval_min(parts[2])
        if interval is None:
            reply(
                chat_id,
                "Bad interval. Use e.g. <code>180</code>, <code>90m</code>, "
                "<code>3h</code> or <code>1d</code> (min 15 minutes).",
            )
            return
        command = " ".join(parts[3:]).strip()
        if not command.startswith("/"):
            reply(chat_id, "The command must start with / (e.g. <code>/scan500</code>).")
            return
        if command.lower().split()[0] in ("/sched",):
            reply(chat_id, "You cannot schedule /sched itself.")
            return
        storage.add_schedule_entry(interval, [command], str(config.TELEGRAM_CHAT_ID))
        log.info("chat %s added schedule entry: every %d min -> %s", chat_id, interval, command)
        reply(
            chat_id,
            f"Added: <code>{html.escape(command)}</code> every "
            f"<b>{interval} min</b>.\n\n{format_schedule(chat_id)}",
        )
        return

    if sub == "remove":
        if len(parts) < 3:
            reply(chat_id, "Usage: <code>/sched remove &lt;n&gt;</code> (number shown by /sched).")
            return
        try:
            index = int(parts[2]) - 1
        except ValueError:
            reply(chat_id, "Usage: <code>/sched remove &lt;n&gt;</code>")
            return
        entries = storage.load_schedule()
        if index < 0 or index >= len(entries):
            reply(chat_id, "No entry at that number. Run /sched to list them.")
            return
        storage.remove_schedule_entry(index)
        log.info("chat %s removed schedule entry %d", chat_id, index)
        reply(chat_id, f"Removed entry {index + 1}.\n\n{format_schedule(chat_id)}")
        return

    if sub == "clear":
        storage.save_schedule([])
        log.info("chat %s cleared the schedule", chat_id)
        reply(chat_id, "Schedule cleared - no automated reports will run.")
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
                "Your watchlist is empty. Add stocks with /add SYMBOL NSE, "
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
    _reply_messages(chat_id, _split_messages(enriched_report.split("\n")))
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
    if token in ("all", "*", "full", "everything"):
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
                lines.append(f"Current Price: <b>{p_str}</b>  {sign}{change_pct:.2f}%{abs_str}")
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
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /add {raw_sym} NSE</i>")
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
                lines.append(f"Current Price: <b>{p_str}</b>  <b>{sign}{change_pct:.2f}%</b>{abs_str}")
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
    lines.append("  \u00b7  ".join(val))
    if fund.get("market_cap") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")
    elif fund.get("mcap_cr") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['mcap_cr']:,.0f}Cr</b>")
    if fund.get("enterprise_value") is not None:
        lines.append(f"Enterprise Value: <b>{_cr(fund['enterprise_value'])}</b>")
    lines.append("")

    # Section 3: Growth & margins (YoY)
    grow = []
    if fund.get("earnings_growth") is not None:
        grow.append(f"Earnings: <b>{_pct(fund['earnings_growth'])}</b>")
    if fund.get("revenue_growth") is not None:
        grow.append(f"Revenue: <b>{_pct(fund['revenue_growth'])}</b>")
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
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /add {raw_sym} NSE</i>")
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
            "Usage: <code>/stock SYMBOL</code> (e.g. <code>/stock TATATECH</code>) "
            "or <code>/stock 5</code> / <code>/stock 5-10</code> (watchlist positions)",
        )
        return

    rng = _parse_stock_range(parts[1])
    if rng is not None:
        handle_stock_batch(chat_id, "/stock", rng, deep=False)
        return

    raw_sym = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    t0 = monotonic()
    log.info("handle_single_stock: fetching full details for %s for chat %s", raw_sym, chat_id)

    quote = sources.get_quote("NSE", raw_sym) or sources.get_quote("BSE", raw_sym) or {}
    fund = sources.get_fundamentals(raw_sym, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        _reply_suggestions(chat_id, raw_sym, "stock")
        return

    lines = _stock_summary_lines(raw_sym, quote, fund, include_tip=True)
    _reply_messages(chat_id, ["\n".join(lines)])
    log.info("handle_single_stock: completed for %s in %.1fs", raw_sym, monotonic() - t0)


def handle_stock_batch(chat_id, cmd: str, rng, deep: bool) -> None:
    """Render /stock or /fund for a range of the user's watchlist positions."""
    cap = MAX_FUND_BATCH if deep else MAX_STOCK_BATCH
    items = storage.get_user_list(chat_id)
    if not items:
        reply(chat_id, "Your watchlist is empty. Add stocks with /add SYMBOL NSE")
        return
    start, end = rng
    total = len(items)
    start = max(1, start)
    end = total if end is None else min(end, total)
    if start > total:
        reply(chat_id, f"Your watchlist has only {total} stock(s) — start position must be 1..{total}.")
        return
    work = items[start - 1:end]
    skipped = max(0, len(work) - cap)
    work = work[:cap]
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

    header = f"\U0001F4CA <b>{cmd.upper()} \u00b7 Watchlist positions {start}\u2013{start + len(work) - 1} of {total}</b>\n"
    all_lines = [header] + body
    if skipped:
        all_lines.append(f"\u2026 and {skipped} more (max {cap} per query).")
    _reply_messages(chat_id, _split_messages(all_lines))
    log.info("%s batch: done %d stocks in %.1fs", cmd, len(work), monotonic() - t0)


def handle_fund_analysis(chat_id, parts) -> None:
    """Deep fundamental report for one symbol, or a watchlist position range.

    /fund RELIANCE  → single symbol
    /fund 5         → first 5 watchlist stocks
    /fund 5-10      → watchlist positions 5..10
    /fund all       → whole watchlist
    """
    if len(parts) < 2:
        reply(
            chat_id,
            "Usage: <code>/fund SYMBOL</code> (e.g. <code>/fund RELIANCE</code>) "
            "or <code>/fund 5</code> / <code>/fund 5-10</code> (watchlist positions)",
        )
        return

    rng = _parse_stock_range(parts[1])
    if rng is not None:
        handle_stock_batch(chat_id, "/fund", rng, deep=True)
        return

    raw_sym = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    t0 = monotonic()
    log.info("handle_fund: deep fundamentals for %s (chat %s)", raw_sym, chat_id)

    quote = sources.get_quote("NSE", raw_sym) or sources.get_quote("BSE", raw_sym) or {}
    fund = sources.get_fundamentals(raw_sym, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        _reply_suggestions(chat_id, raw_sym, "fund")
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
        {"command": "ca", "description": "Corporate actions all NSE+BSE: /ca [type]"},
        {"command": "exdate", "description": "Ex-dates: /exdate today or /exdate 7"},
        {"command": "summary", "description": "Market snapshot: counts + next ex-dates"},
        {"command": "news", "description": "Latest news for your watchlist stocks"},
        {"command": "stock", "description": "Stock summary or watchlist range: /stock 5-10"},
        {"command": "fund", "description": "Deep fundamentals or range: /fund 3-5"},
        {"command": "harmonic", "description": "Harmonic scan NIFTY 100/500: /harmonic all / 500"},
        {"command": "scan500", "description": "NIFTY 500 CNC/MIS technical scanner"},
        {"command": "movers", "description": "Movers + fundamentals: /movers 1h gainers 10"},
        {"command": "gainers", "description": "Top gainers + fundamentals: /gainers 1h 50"},
        {"command": "losers", "description": "Top losers + fundamentals: /losers 1w 100"},
        {"command": "add", "description": "Add stock to watchlist: /add RELIANCE NSE"},
        {"command": "remove", "description": "Remove stock from watchlist"},
        {"command": "list", "description": "Show your watchlist"},
        {"command": "next", "description": "Upcoming ex-dates for your watchlist"},
        {"command": "filter", "description": "Receive only chosen action types"},
        {"command": "alert", "description": "Price-move alert threshold percent"},
        {"command": "settings", "description": "Show your current settings"},
        {"command": "sched", "description": "Schedule auto reports: /sched add 3h /scan500"},
        {"command": "status", "description": "Check persistence / GitHub push"},
        {"command": "checknow", "description": "Force a check and resend alerts"},
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


def start_scheduled_reports():
    """Run scheduled reports to the owner chat on a timer (daemon thread).

    Only the always-on server runs these (PROCESS_COMMANDS=true); the GitHub
    Actions cron and any other process skip them so scans are never sent
    twice. The first report fires a short while after startup so the server
    has finished booting before the scans hit the data feeds.

    Entries come from schedule.json (manageable from Telegram with /sched);
    when the file is empty the env-var defaults (SCHEDULED_COMMANDS +
    SCHEDULED_REPORTS_INTERVAL_MIN) are used so existing deployments keep
    working. The schedule is re-read each loop, so /sched add/remove/clear
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
        entries = storage.load_schedule()
        if entries:
            return entries
        cmds = [c for c in config.SCHEDULED_COMMANDS if c.strip()]
        if not cmds:
            return []
        return [{
            "interval_min": config.SCHEDULED_REPORTS_INTERVAL_MIN,
            "commands": cmds,
            "chat": default_chat,
        }]

    next_due = {}  # index -> monotonic() seconds when the next run is due

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
                for idx, entry in enumerate(entries):
                    interval = int(entry.get("interval_min") or config.SCHEDULED_REPORTS_INTERVAL_MIN)
                    commands = [c for c in entry.get("commands") or [] if c.strip()]
                    chat = str(entry.get("chat") or default_chat)
                    if not commands:
                        continue
                    due = next_due.get(idx)
                    if due is None:
                        next_due[idx] = now + min(interval * 60, 60)
                        continue
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
                    next_due[idx] = monotonic() + interval * 60
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
