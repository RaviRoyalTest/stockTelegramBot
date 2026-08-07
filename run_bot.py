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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
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

HELP_TEXT = (
    "\U0001F4CA <b>Corporate Action Alerts</b>\n"
    "<i>NSE + BSE alerts, market screens and news - right in Telegram.</i>\n\n"
    "------------------------------------\n"
    "\U0001F4C8 <b>Market</b>\n"
    "/ca [type|SYMBOL|N|today] \u2014 corporate actions, all NSE + BSE\n"
    "   \u2022 /ca dividend \u00b7 /ca bonus \u00b7 /ca split \u00b7 /ca rights \u00b7 /ca buyback\n"
    "   \u2022 /ca increase \u2014 shareholder increase (bonus + split + rights)\n"
    "   \u2022 /ca today \u00b7 /ca 7 \u2014 ex-date today / within 7 days\n"
    "   \u2022 /ca RELIANCE \u2014 full details for one symbol\n"
    "   \u2022 /ca TATA \u2014 keyword search in company / subject\n"
    "/exdate [today|N] \u2014 all actions by ex-date window (default 5 days)\n"
    "/summary \u2014 counts by exchange &amp; type, plus next ex-dates\n"
    "/movers | /gainers | /losers [period] [direction] [N] [100|500]\n"
    "   \u2014 movement screens over NIFTY 100 or NIFTY 500 stocks\n"
    "   \u2022 /movers 1h gainers 10 500 \u00b7 /gainers 2d 100 (top 100)\n"
    "   \u2022 /losers 1mo 100 (top 100) \u00b7 /losers 30m 5 nifty100\n"
    "   \u2022 /movers 500 (NIFTY 500) \u00b7 /gainers 1w nifty500\n\n"
    "\u2B50 <b>Watchlist</b>\n"
    "/add SYMBOL [NSE|BSE] \u2014 add a stock you hold\n"
    "/remove SYMBOL \u2014 remove a stock\n"
    "/list \u2014 show your watchlist\n"
    "/next \u2014 upcoming ex-dates for your watchlist\n"
    "/news [N|SYMBOL] \u2014 latest headlines for your stocks\n"
    "   \u2022 /news \u00b7 /news 5 \u00b7 /news RELIANCE\n\n"
    "\u2699\ufe0f <b>Personalize</b>\n"
    "/filter TYPE,TYPE \u2014 only receive chosen action types\n"
    "   \u2022 types: dividend, bonus, split, rights, buyback (/filter all resets)\n"
    "   \u2022 /filter dividend,bonus \u00b7 /filter all\n"
    "/alert PCT \u2014 alert when a stock moves \u00b1PCT% in a day (/alert off)\n"
    "   \u2022 /alert 3 \u00b7 /alert 1.5 \u00b7 /alert off\n"
    "/settings \u2014 show your current filters &amp; alert settings\n\n"
    "\U0001F6E0\ufe0f <b>System</b>\n"
    "/status \u2014 where your list is saved &amp; GitHub push status\n"
    "/checknow \u2014 force a check and re-send your alerts\n"
    "/help \u00b7 /start \u2014 this message\n\n"
    "------------------------------------\n"
    "\U0001F4A1 <b>Tips</b>\n"
    "\u2022 Just ask in plain text: \u201ccorporate action\u201d, \u201cshareholder "
    "increase\u201d, \u201cdividends\u201d, \u201cex-date today\u201d, \u201cgainers\u201d, \u201cnews\u201d.\n"
    "\u2022 Periods for /movers, /gainers, /losers:\n"
    "   intraday 5m \u00b7 15m \u00b7 30m \u00b7 1h \u00b7 2h \u00b7 4h\n"
    "   daily 1d(today) \u00b7 2d \u00b7 3d \u00b7 5d \u00b7 7d \u00b7 1w \u00b7 2w \u00b7 1mo \u00b7 3mo \u00b7 6mo \u00b7 1y\n"
    "\u2022 Index: /movers 100 or 500, or the nifty100 / nifty500 keywords, pick the universe.\n"
    "\u2022 For /gainers and /losers a bare 100 or 500 is a count (top N); use nifty100/nifty500 for the index.\n"
    "\u2022 Direction: gainers / losers / all; count 1-100 (gainers/losers default 30).\n"
    "\u2022 Each stock shows P/E, sector P/E, 52-week high/low, dividend yield,\n"
    "   promoter/FII/DII holding and debt/equity when available.\n"
    "\u2022 Type / alone to see this help again.\n\n"
    "\U0001F4C5 <b>Examples</b>\n"
    "<b>Watchlist:</b>  /add RELIANCE NSE  \u00b7  /add PGINVIT NSE  \u00b7  /remove TCS\n"
    "<b>Corporate actions:</b>  /ca  \u00b7  /ca dividend  \u00b7  /ca increase  \u00b7  /ca 7  \u00b7  /ca RELIANCE\n"
    "<b>Ex-dates:</b>  /exdate today  \u00b7  /exdate 10  \u00b7  /next\n"
    "<b>Movers:</b>  /movers 30m  \u00b7  /movers 1h gainers 10 500  \u00b7  /movers 2d  \u00b7  /movers 1w 500\n"
    "<b>Gainers:</b>  /gainers  \u00b7  /gainers 50  \u00b7  /gainers 2d 100  \u00b7  /gainers 1h nifty500\n"
    "<b>Losers:</b>  /losers  \u00b7  /losers 1mo 100  \u00b7  /losers 30m 5 nifty100  \u00b7  /losers 1w nifty500\n"
    "<b>News:</b>  /news  \u00b7  /news 5  \u00b7  /news RELIANCE\n"
    "<b>Personalize:</b>  /filter dividend,bonus  \u00b7  /alert 3  \u00b7  /settings"
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
    return resp.json().get("result", [])


def reply(chat_id, text):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=config.HTTP_TIMEOUT)


def send_help(chat_id):
    """Send the styled HTML help message (/help, /start, unknown commands)."""
    try:
        notifier.send_message(HELP_TEXT, chat_id=chat_id)
    except notifier.NotifierError as exc:
        log.warning("help send failed for chat %s: %s", chat_id, exc)
        reply(chat_id, "Could not send help. Use /help later.")


def github_push_configured() -> bool:
    """True only when the host can actually push state back to GitHub."""
    return bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


# ----------------------------------------------------------------- watchlist
def handle_command(chat_id, text):
    parts = (text or "").strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    log.info("command from chat %s: %s", chat_id, text)

    if cmd in ("/start", "/help", "/"):
        send_help(chat_id)
        return

    if cmd == "/list":
        items = storage.get_user_list(chat_id)
        if not items:
            reply(chat_id, "Your watchlist is empty.")
        else:
            lines = [f"{i['symbol']} ({i['exchange']})" for i in items]
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
                "Your watchlist:\n"
                + "\n".join(lines)
                + f"\n\nSaved in: {where} - {persistence}",
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
            reply(chat_id, f"Could not fetch corporate actions: {exc}")
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
                "Current filters: " + (", ".join(current) if current else "all types")
                + "\nUsage: /filter dividend,bonus  or  /filter all",
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
        msg = "Filters set to: " + (", ".join(chosen) if chosen else "all types")
        if bad:
            msg += f"\nIgnored unknown type(s): {', '.join(bad)}"
            msg += f" (valid: {', '.join(sources.ACTION_TYPES)})"
        reply(chat_id, msg)
        return

    if cmd == "/alert":
        settings = storage.get_user_settings(chat_id)
        current = settings.get("price_alert_pct")
        if len(parts) < 2:
            if current:
                reply(chat_id, f"Current price-alert threshold: {current:g}%")
            else:
                reply(chat_id, "Price alerts are off.\nUsage: /alert 3  (percent move)  or  /alert off")
            return
        raw = parts[1].lower()
        if raw in ("off", "none", "0", "0%"):
            val = None
        else:
            try:
                val = abs(float(raw.strip().rstrip("%")))
            except ValueError:
                reply(chat_id, "Usage: /alert 3  (e.g. 3%)  or  /alert off")
                return
            if val == 0:
                val = None
        settings["price_alert_pct"] = val
        storage.save_user_settings(chat_id, settings)
        log.info(
            "chat %s price-alert threshold set to: %s",
            chat_id, "off" if val is None else f"{val:g}%",
        )
        reply(chat_id, f"Price alerts {'off' if val is None else 'set to ' + format(val, 'g') + '%'}.")
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
                    f"Your chat id: {chat_id}",
                    f"Role: {'owner' if owner else 'subscriber'}",
                    f"Your list is saved in: {location}",
                    f"GitHub push: {push_status}",
                    sync_line,
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
        reply(chat_id, "Usage: /add SYMBOL [NSE|BSE]  or  /remove SYMBOL [NSE|BSE]")
        return

    symbol = parts[1].upper()
    exchange = (parts[2].upper() if len(parts) > 2 else "NSE")
    exchange = exchange if exchange in ("NSE", "BSE") else "NSE"

    if cmd == "/add":
        quote = sources.get_quote(exchange, symbol)
        company = quote.get("name", "") if quote else ""
        validated = quote is not None
        if not validated and exchange == "NSE":
            # Yahoo can be flaky from datacenter IPs (e.g. Render). Fall back
            # to the NSE stock list so valid tickers still get added even when
            # the live quote is unavailable.
            exact = next(
                (
                    s for s in sources.search_stocks(symbol, limit=5)
                    if s["symbol"].upper() == symbol
                ),
                None,
            )
            if exact is not None:
                company = exact["company"]
                validated = True
                log.info(
                    "Yahoo quote unavailable for %s:%s; validated via NSE stock list",
                    exchange, symbol,
                )
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
            f"Added {symbol} ({exchange}). Alerts will come to this chat.\n"
            f"Saved in: {where}.",
        )
    elif cmd == "/remove":
        storage.remove_from_user_list(chat_id, symbol, exchange)
        log.info("Removed %s (%s) for chat %s", symbol, exchange, chat_id)
        reply(chat_id, f"Removed {symbol} ({exchange}) if it was present.")
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
    for action, quote in results:
        if quote:
            action["quote"] = quote


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
    try:
        all_actions, errors, warnings = poller_mod.fetch_all_actions()
    except Exception as exc:
        reply(chat_id, f"Could not fetch corporate actions: {config.redact(exc)}")
        return True
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
                         default_period=("intraday", 60), default_count=None,
                         default_universe="nifty100") -> None:
    """Screen an index universe by price movement over a time window.

    One implementation backs /movers, /gainers and /losers so all three stay
    feature-identical. Every command understands the shared options:
      /movers 1h gainers 10 500   period, direction, count, index universe
      /gainers 2d 100            top 100 gainers over 2 days
      /losers 30m 5 500          top 5 losers over 30 min, NIFTY 500
    Gainers/losers sort by size (best first); the movers "all" view sorts
    lower -> higher.
    """
    period, direction, count, universe = _parse_screen_parts(
        parts, default_period, default_direction, default_count,
        default_universe)

    universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
    period_label = _period_label(*period)
    log.info(
        "screen %s: period=%s direction=%s count=%s universe=%s",
        parts[0], period_label, direction, count, universe_label,
    )

    symbols = sources.get_index_universe(universe)
    if not symbols:
        log.warning("screen %s: no symbols loaded for universe %s", parts[0], universe)
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return

    def _fetch(sym):
        return sym, _fetch_period_change(sym, period)

    with ThreadPoolExecutor(max_workers=20) as ex:
        fetched = list(ex.map(_fetch, symbols))

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
    if not rows:
        ok = sum(1 for _, d in fetched if d and d.get("change_pct") is not None)
        log.warning(
            "screen %s: no %s in %s over %s (universe=%d, quotes ok=%d/%d) - "
            "market may be closed or everything moved the other way",
            parts[0], direction, universe_label, period_label,
            len(symbols), ok, len(fetched),
        )
        reply(chat_id, f"No movement data found for {period_label} ({universe_label}).")
        return

    def _fund_fetch(sym, with_screener):
        return sym, sources.get_fundamentals(sym, with_screener=with_screener)

    fund_by_sym = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        tasks = [
            (sym, i < sources.FUND_MAX_ROWS)
            for i, (sym, _) in enumerate(rows)
        ]
        for sym, fund in ex.map(lambda t: _fund_fetch(t[0], t[1]), tasks):
            fund_by_sym[sym] = fund

    failed = len(fetched) - sum(1 for _, d in fetched if d and d.get("change_pct") is not None)
    header = f"{title} · {universe_label}"
    if count:
        header += f" · top {len(rows)}"
    lines = [header]
    for idx, (sym, d) in enumerate(rows, 1):
        change = d["change_pct"]
        arrow = "\u25b2" if change >= 0 else "\u25bc"
        sign = "+" if change >= 0 else ""
        lines.append(
            f"{idx}. {arrow} <b>{notifier.escape(sym)}</b> "
            f"{notifier.fmt_money(d['price'])} <b>{sign}{change:.2f}%</b>"
        )
        fund_line = _fundamentals_line(fund_by_sym.get(sym))
        if fund_line:
            lines.append("   " + fund_line)
    if len(rows) > sources.FUND_MAX_ROWS:
        lines.append(
            f"(fundamentals detail shown for the first {sources.FUND_MAX_ROWS} stocks)"
        )
    if failed:
        lines.append(f"({failed} of {len(symbols)} stocks could not be loaded)")
    _reply_messages(chat_id, _split_messages(lines))
    log.info(
        "screen %s: replied %d row(s), %d quote(s) failed, fundamentals cached=%d",
        parts[0], len(rows), failed, len(fund_by_sym),
    )


def _fundamentals_line(fund: dict | None) -> str:
    """One compact fundamentals line for a stock, or '' when nothing to show."""
    if not fund:
        return ""

    def _num(value, nd: int) -> str:
        s = f"{value:.{nd}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    parts = []
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
