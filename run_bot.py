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
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from time import monotonic
from pathlib import Path

from corp_actions import config  # no third-party deps - always importable

try:
    import requests

    import corp_actions.poller as poller_mod
    from corp_actions import notifier, sources, storage
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
    "\U0001F4C8 <b>Market Screens</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/movers <i>[period] [N] [100|500]</i>\n"
    "  Top movers (up &amp; down) in a time window.\n"
    "  /movers          \u2192 last 1h, NIFTY 100\n"
    "  /movers 30m      \u2192 last 30 minutes\n"
    "  /movers 2d 500   \u2192 2-day movers, NIFTY 500\n"
    "  /movers 1w 10    \u2192 top 10 movers this week\n\n"
    "/gainers <i>[period] [N] [100|500]</i>\n"
    "  Top rising stocks. Default: today, NIFTY 500, top 15.\n"
    "  /gainers             \u2192 today's top gainers\n"
    "  /gainers 1h          \u2192 last 1h gainers\n"
    "  /gainers 1mo 20 500  \u2192 top 20 gainers this month, NIFTY 500\n"
    "  /gainers 3mo nifty100\u2192 3-month gainers, NIFTY 100\n\n"
    "/losers <i>[period] [N] [100|500]</i>\n"
    "  Top falling stocks. Default: today, NIFTY 500, top 15.\n"
    "  /losers             \u2192 today's top losers\n"
    "  /losers 1h 10       \u2192 top 10 losers last hour\n"
    "  /losers 1mo 500     \u2192 biggest losers this month, NIFTY 500\n"
    "  /losers 1w nifty100 \u2192 weekly losers, NIFTY 100\n\n"
    "Periods: 5m \u00b7 15m \u00b7 30m \u00b7 1h \u00b7 2h \u00b7 4h \u00b7 1d \u00b7 2d \u00b7 5d \u00b7 1w \u00b7 2w \u00b7 1mo \u00b7 3mo \u00b7 6mo \u00b7 1y\n"
    "Universe: 100/nifty100=NIFTY 100 \u00b7 500/nifty500=NIFTY 500\n\n"
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
    "/next                \u2192 upcoming ex-dates for your watchlist\n"
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
    "/checknow            \u2192 force-run alerts and re-send all matches\n"
    "/help \u00b7 /start       \u2192 show this guide\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "\U0001F4A1 <b>Quick Examples</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "/gainers 1h 10          \u2192 Top 10 gainers last hour\n"
    "/losers 1mo 500         \u2192 Monthly losers \u2014 NIFTY 500\n"
    "/movers 2d 10 500       \u2192 2-day movers, top 10, NIFTY 500\n"
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
    reply(chat_id, HELP_TEXT)


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
            lines = [f"• <b>{i['symbol']}</b> ({i['exchange']})" for i in items]
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
                + f"\n\nSaved in: <code>{html.escape(where)}</code>\nPersistence: {html.escape(persistence)}",
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
        reply(chat_id, notifier.format_upcoming_list(upcoming))
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
        else:
            push_status = (
                "NOT set - your changes stay only on this host's disk (lost "
                "on redeploy). Set GH_TOKEN + GITHUB_REPOSITORY on this host."
            )
            sync_line = "Local state vs GitHub: unknown (no GitHub credentials)"
        reply(
            chat_id,
            "\n".join(
                [
                    f"<b>Your chat id:</b> <code>{chat_id}</code>",
                    f"<b>Role:</b> {'owner' if owner else 'subscriber'}",
                    f"<b>Saved in:</b> <code>{html.escape(location)}</code>",
                    f"<b>GitHub push:</b> {html.escape(push_status)}",
                    html.escape(sync_line),
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

    if len(parts) < 2:
        reply(chat_id, "Usage: <code>/add SYMBOL [NSE|BSE]</code> or <code>/remove SYMBOL [NSE|BSE]</code>")
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


def _reply_suggestions(chat_id, query):
    """Reply with matching stocks from the NSE list when an exact symbol fails."""
    matches = sources.search_stocks(query, limit=10)
    if not matches:
        log.info(
            "No stock matched '%s' for chat %s - nothing added", query, chat_id
        )
        reply(chat_id, f"No stocks match '{query}'.")
        return
    lines = [f"'{query}' not found as an exact symbol. Did you mean (NSE):"]
    for m in matches:
        company = m["company"] or ""
        lines.append(f"  /add {m['symbol']} NSE  - {company}")
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
        today = date.today()
        cutoff = today + timedelta(days=days)
        results = [
            a for a in all_actions
            if (d := poller_mod.parse_ex_date(a.get("ex_date"))) and today <= d <= cutoff
        ]
        label = "today" if days == 0 else f"within {days} day(s)"
        title = f"<b>Ex-date {label}</b> (NSE + BSE)"

    else:  # mode == "term": exact symbol first, then keyword search
        term = descriptor.get("term", "").strip()
        symbol_matches = [
            a for a in all_actions
            if (a.get("symbol") or "").upper() == term.upper()
        ]
        if symbol_matches:
            _attach_quotes(symbol_matches)
            messages = [f"<b>Corporate actions for {notifier.escape(term.upper())}</b>"]
            for a in sorted(symbol_matches, key=lambda x: x.get("ex_date") or "9999-99-99"):
                messages.append(notifier.format_action_detail(a))
            _reply_messages(chat_id, messages)
            return True
        q = term.lower()
        results = [
            a for a in all_actions
            if q in (a.get("company") or "").lower()
            or q in (a.get("subject") or "").lower()
        ]
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
      universe  nifty100 / nifty500 keywords, or a second number after a count

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
        elif t in ("100", "nifty100", "nifty-100", "nifty 100"):
            if t == "100" and not explicit_count and default_count is not None:
                count = 100
                explicit_count = True
            else:
                universe = "nifty100"
        elif t in ("500", "allstocks", "all-stocks", "nifty500", "nifty-500",
                   "nifty 500"):
            if t == "500" and not explicit_count and default_count is not None:
                count = 100
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


def handle_market_screen(chat_id, parts, default_direction="all",
                         default_period=("intraday", 60), default_count=15,
                         default_universe="nifty100") -> None:
    """Screen an index universe by price movement over a time window.

    Responds concisely with the top results without blocking command processing.
    """
    period, direction, count, universe = _parse_screen_parts(
        parts, default_period, default_direction, default_count,
        default_universe)

    # Limit maximum rows to 25 so responses are concise and fast
    count = min(count or 15, 25)

    universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
    period_label = _period_label(*period)
    t0 = monotonic()
    log.info(
        "screen %s: period=%s direction=%s count=%d universe=%s",
        parts[0], period_label, direction, count, universe_label,
    )

    symbols = sources.get_index_universe(universe)
    if not symbols:
        log.warning("screen %s: no symbols loaded for universe %s", parts[0], universe)
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return

    def _fetch(sym):
        return sym, _fetch_period_change(sym, period)

    fetched = []
    with ThreadPoolExecutor(max_workers=25) as ex:
        futures = {ex.submit(_fetch, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                data = fut.result()[1]
            except Exception:
                data = None
            fetched.append((sym, data))

    rows = [(sym, d) for sym, d in fetched if d and d.get("change_pct") is not None]
    if direction == "gainers":
        rows = [r for r in rows if r[1]["change_pct"] > 0]
        rows.sort(key=lambda r: r[1]["change_pct"], reverse=True)
        title = f"<b>Top Gainers - {period_label}</b>"
    elif direction == "losers":
        rows = [r for r in rows if r[1]["change_pct"] < 0]
        rows.sort(key=lambda r: r[1]["change_pct"])
        title = f"<b>Top Losers - {period_label}</b>"
    else:
        rows.sort(key=lambda r: r[1]["change_pct"])
        title = f"<b>Movers - {period_label}</b> · {direction}"

    rows = rows[:count]
    if not rows:
        reply(chat_id, f"No movement data found for {period_label} ({universe_label}).")
        return

    header = f"{title} · {universe_label} (Top {len(rows)})"

    # Fast fundamentals (Yahoo Finance only - no screener.in scraping to prevent blocking)
    def _fund_fetch(sym):
        return sym, sources.get_fundamentals(sym, with_screener=False)

    fund_by_sym = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fund_fetch, sym): sym for sym, _ in rows}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                fund_by_sym[sym] = fut.result()[1]
            except Exception:
                fund_by_sym[sym] = None

    lines = [header]
    for idx, (sym, d) in enumerate(rows, 1):
        change = d["change_pct"]
        price = d.get("price")
        fund = fund_by_sym.get(sym)
        # Color circle + arrow based on direction and magnitude
        if change >= 3.0:
            move_icon = "\U0001F7E2\u25b2\u25b2"   # 🟢▲▲ strong up
        elif change >= 1.0:
            move_icon = "\U0001F7E2\u25b2"          # 🟢▲ up
        elif change <= -3.0:
            move_icon = "\U0001F534\u25bc\u25bc"   # 🔴▼▼ strong down
        elif change <= -1.0:
            move_icon = "\U0001F534\u25bc"          # 🔴▼ down
        elif change >= 0:
            move_icon = "\U0001F7E1\u25b2"          # 🟡▲ small up
        else:
            move_icon = "\U0001F7E1\u25bc"          # 🟡▼ small down
        sign = "+" if change >= 0 else ""
        chg_str = f"{sign}{change:.2f}%"
        # 52-week zone signal emoji
        sig_emoji, _ = _wk52_signal(price, fund)
        sig_prefix = f" {sig_emoji}" if sig_emoji else ""
        lines.append(
            f"{idx}. {move_icon}{sig_prefix} <b>{notifier.escape(sym)}</b>  "
            f"{notifier.fmt_money(price)}  <b>{chg_str}</b>"
        )
        fund_line = _fundamentals_line(fund, price)
        if fund_line:
            lines.append("   " + fund_line)

    _reply_messages(chat_id, _split_messages(lines))
    log.info(
        "screen %s: completed %d rows in %.1fs",
        parts[0], len(rows), monotonic() - t0,
    )


def _wk52_signal(price, fund: dict | None) -> tuple:
    """Return (signal_emoji, range_tag) based on 52-week position of price.

    Thresholds:
      0-15%  from low  -> ✅ Strong Buy (near 52W low)
      15-35% from low  -> 📈 Buy Zone
      35-65% from low  -> 🟡 Mid-Range
      65-85% from low  -> ⚠️ High Zone
      85-100%from low  -> 🚫 Avoid (near 52W high)
    """
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


def _fundamentals_line(fund: dict | None, price=None) -> str:
    """Compact fundamentals line with 52-week signal for a stock, or '' when nothing to show."""
    if not fund:
        return ""

    def _num(value, nd: int) -> str:
        s = f"{value:.{nd}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    sig_emoji, range_tag = _wk52_signal(price, fund)
    parts = []
    if range_tag:
        parts.append(range_tag)
    if fund.get("pe"):
        parts.append(f"P/E {_num(fund['pe'], 1)}")
    if fund.get("sector_pe"):
        parts.append(f"Sec P/E {_num(fund['sector_pe'], 1)}")
    if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
        parts.append(
            f"52w {notifier.fmt_money(fund['wk52_low'])}\u2013"
            f"{notifier.fmt_money(fund['wk52_high'])}"
        )
    if fund.get("div_yield") is not None:
        parts.append(f"Div {_num(fund['div_yield'], 2)}%")
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct")):
        bits = []
        for key, label in (("promoter_pct", "Prom"), ("fii_pct", "FII"), ("dii_pct", "DII")):
            if fund.get(key):
                bits.append(f"{label} {fund[key]}")
        parts.append(" \u00b7 ".join(bits))
    if fund.get("debt_to_equity") is not None:
        parts.append(f"D/E {_num(fund['debt_to_equity'], 2)}")
    return " | ".join(parts)


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
        handle_command(chat_id, text)
    if max_offset:
        # Mark updates as consumed.
        get_updates(offset=max_offset + 1)
    return checknow_chat


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


def push_state() -> bool:
    """Commit and push watchlist/seen state back to the repo, if changed.

    Returns True when the repo is in sync (pushed, or nothing to push).
    Returns False when credentials are missing or the push failed - callers
    should NOT discard local state in that case.

    Handles the expected race with the hourly cron (both push to the same
    branch): on a rejected push it fetches, rebases onto the remote and
    retries once.
    """
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
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
    added = _git("git", "add", *[str(f) for f in STATE_FILES])
    if added.returncode != 0:
        log.warning(
            "git add failed - state NOT pushed (local changes kept): %s",
            added.stderr.strip()[-300:],
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
                return True
            log.warning(
                "Retry push of existing local commits failed: %s",
                push.stderr.strip()[-300:],
            )
            return False
        log.info("No state change to push")
        return True
    log.info(
        "Staged state files: %s", ", ".join(staged.splitlines())
    )

    commit = _git("git", "commit", "-m", "chore: update watchlist from Telegram")
    if commit.returncode != 0:
        # Keep the changes in the worktree instead of the index so a later
        # sync (reset --hard) refuses to wipe them.
        log.warning("State commit failed: %s", commit.stderr.strip()[-300:])
        _git("git", "reset")
        return False

    push = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push.returncode == 0:
        log.info("Pushed state to %s", branch)
        return True

    # Expected race with the cron: retry once after rebasing onto remote.
    _git("git", "fetch", "origin")
    rebase = _git("git", "rebase", f"origin/{branch}")
    if rebase.returncode != 0:
        _git("git", "rebase", "--abort")
        log.warning(
            "Push failed and rebase aborted (conflict): %s",
            push.stderr.strip()[-300:],
        )
        return False
    push2 = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push2.returncode == 0:
        log.info("Pushed state to %s (after rebase)", branch)
        return True
    log.warning("Push failed after rebase: %s", push2.stderr.strip()[-500:])
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
        log.warning("State sync failed: %s", res.stderr.strip()[-300:])
        return False
    except Exception as exc:
        log.warning("State sync failed: %s", exc)
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
            print(f"  read  (ls-remote)    : FAILED - {ls.stderr.strip()[-200:]}")
            ok = False
        dry = _git("git", "push", "--dry-run", url, "HEAD:refs/heads/__state_check__")
        if dry.returncode == 0:
            print(
                "  write (push dry-run) : OK - a push would be accepted "
                "(no branch created)"
            )
        else:
            print(f"  write (push dry-run) : FAILED - {dry.stderr.strip()[-300:]}")
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
