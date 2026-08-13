"""Command dispatcher: routes commands, natural-language queries and callbacks.

One responsibility: turn an incoming Telegram update into a call into the
command-family modules (corporate_action_commands, watchlist_commands, settings_commands, ...). The
routing tables live in registry.py; adding a command means adding a handler
module + a registry entry, never editing this file's dispatch chain.
"""
from __future__ import annotations

import html
import logging
from time import monotonic

from .. import config, storage
from ..core.text import escape, split_messages
from ..formatting.schedule import format_settings
from ..sources.types import INCREASE_TYPES
from ..telegram.client import answer_callback_query
from ..telegram.markup import quick_menu_markup
from . import (
    checklist_commands,
    corporate_action_commands,
    forecast_commands,
    fundamentals_commands,
    gappers_commands,
    harmonic_commands,
    indicator_commands,
    learn_commands,
    movers_commands,
    scanner_commands,
    schedule_commands,
    settings_commands,
    status as status_commands,
    us_commands,
    watchlist_commands,
)
from .help_texts import ALL_COMMANDS_TEXT, CA_HELP
from .registry import ALIAS_TO_MAIN, _bare_command_usage, send_help
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

# Commands that mutate state on disk - /status uses this to decide whether a
# GitHub push is warranted after handling a message.
WRITE_COMMANDS = {
    "/add", "/addstock", "/remove", "/removestock",
    "/filter", "/alertfilters", "/actionfilters", "/alert", "/pricealert",
    "/sched", "/schedule", "/watcher", "/bigmover", "/moverwatch",
    "/myfavourites", "/favorites", "/favourites", "/mypicks", "/dailybrief",
    "/moversfund", "/market",
}


def handle_command(chat_id, text):
    """Route one command text to the right command-family handler."""
    parts = (text or "").strip().split()
    if not parts:
        return
    command = parts[0].lower().split("@")[0]
    log.info("command from chat %s: %s", chat_id, text)

    if command in ("/start", "/help", "/"):
        send_help(chat_id)
        return

    if command == "/all":
        reply_messages(
            chat_id,
            split_messages(ALL_COMMANDS_TEXT.split("\n")),
            reply_markup=quick_menu_markup(),
        )
        return

    # Bare main command -> list its subcommands (like /watcher does). Only
    # fires when NO arguments were given, so nothing with arguments changes.
    # Aliases map to their canonical command first, so /next, /summary, /ca,
    # /stock, /fund, /sched etc. show the same hints as their main names.
    if len(parts) == 1:
        canonical = ALIAS_TO_MAIN.get(command, command)
        if canonical != command and _bare_command_usage(chat_id, canonical):
            return
        if _bare_command_usage(chat_id, command):
            return

    if command in ("/list", "/watchlist"):
        watchlist_commands.send_watchlist(chat_id)
        return

    if command == "/checknow":
        reply(chat_id, "Running a forced check now - re-sending all matching alerts shortly.")
        return

    if command in ("/next", "/upcoming", "/corpactionsformylist"):
        watchlist_commands.send_watchlist_actions(chat_id)
        return

    if command in ("/filter", "/alertfilters", "/actionfilters"):
        settings_commands.handle_alertfilters(chat_id, parts)
        return

    if command in ("/alert", "/pricealert"):
        settings_commands.handle_pricealert(chat_id, parts)
        return

    if command in ("/watcher", "/bigmover", "/moverwatch"):
        settings_commands.handle_watcher(chat_id, parts)
        return

    if command == "/moversfund":
        settings_commands.handle_moversfund(chat_id, parts)
        return

    if command == "/status":
        status_commands.handle_status(chat_id)
        return

    if command in ("/ca", "/corpactions", "/corporate-actions", "/corp-actions",
               "/actions", "/shareholder", "/increase"):
        if command in ("/shareholder", "/increase"):
            descriptor = {"mode": "types", "types": list(INCREASE_TYPES)}
        elif len(parts) > 1:
            descriptor = corporate_action_commands.parse_ca_arg(parts[1])
        else:
            descriptor = {"mode": "overview"}
        if descriptor is None:
            reply(chat_id, CA_HELP)
        else:
            corporate_action_commands.run_ca_query(chat_id, descriptor)
        return

    if command in ("/exdate", "/exdates", "/ex-dates"):
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
        corporate_action_commands.run_ca_query(chat_id, {"mode": "exdate", "days": days})
        return

    if command in ("/summary", "/casummary", "/corpactionssummary"):
        corporate_action_commands.run_ca_query(chat_id, {"mode": "overview"})
        return

    if command == "/settings":
        reply(chat_id, format_settings(chat_id))
        return

    if command in ("/myfavourites", "/favorites", "/favourites", "/mypicks", "/dailybrief"):
        watchlist_commands.handle_favourites(chat_id, parts)
        return

    if command in ("/menu", "/quick", "/shortcuts", "/buttons"):
        schedule_commands.handle_menu(chat_id, parts)
        return

    if command in ("/sched", "/schedule", "/schednow"):
        schedule_commands.handle_sched(chat_id, parts)
        return

    if command == "/market":
        schedule_commands.handle_market(chat_id, parts)
        return

    if command == "/news":
        watchlist_commands.handle_news(chat_id, parts)
        return

    if command in ("/movers", "/topmovers", "/marketmovers"):
        movers_commands.handle_movers(chat_id, parts)
        return

    if command in ("/gap", "/gappers"):
        gappers_commands.handle_gappers(chat_id, parts)
        return

    if command in ("/gainers", "/topgainers", "/losers", "/toplosers"):
        direction = "gainers" if command in ("/gainers", "/topgainers") else "losers"
        movers_commands.handle_gainers_losers(chat_id, parts, direction)
        return

    if command in ("/fund", "/fundamentals", "/fundamentalreport"):
        fundamentals_commands.handle_fund_analysis(chat_id, parts)
        return

    if command in ("/harmonic", "/harmonicpatterns"):
        harmonic_commands.handle_harmonic(chat_id, parts)
        return

    if command == "/scan500":
        scanner_commands.handle_scan500(chat_id, parts)
        return

    if command in ("/ind", "/indicator", "/tech", "/technical"):
        indicator_commands.handle_indicator(chat_id, parts)
        return

    if command in ("/analyst", "/forecast", "/forecastanalysis"):
        forecast_commands.handle_forecast(chat_id, parts)
        return

    if command in ("/learn", "/guide", "/explain", "/tutorial", "/howto"):
        learn_commands.handle_learn(chat_id, parts)
        return

    if command in ("/stock", "/info", "/quote", "/stockanalysis", "/stock-analysis",
               "/analysis", "/fundamentalanalyze", "/fundamental-analysis"):
        fundamentals_commands.handle_single_stock_analysis(chat_id, parts)
        return

    if command in ("/us", "/usstock", "/usfund", "/usquote"):
        us_commands.handle_us_stock(chat_id, parts)
        return

    if command in ("/checklist", "/investcheck", "/scorecard", "/qualitycheck", "/quality"):
        checklist_commands.handle_checklist(chat_id, parts)
        return

    if len(parts) < 2:
        if command in ("/add", "/addstock", "/remove", "/removestock"):
            reply(chat_id, "Usage: <code>/addstock SYMBOL [NSE|BSE]</code> or <code>/removestock SYMBOL [NSE|BSE]</code>")
        else:
            reply(chat_id, f"Unknown command <code>{html.escape(command)}</code>. Type <code>/help</code> for the available commands.")
        return

    if command in ("/add", "/addstock", "/remove", "/removestock"):
        watchlist_commands.handle_add_remove(chat_id, parts, command)
        return

    send_help(chat_id)


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
    if not any(keyword in low for keyword in keywords):
        return False
    if "gainers" in low or "gainer" in low:
        movers_commands.handle_gainers_losers(chat_id, ["/gainers"], "gainers")
        return True
    if "losers" in low or "loser" in low:
        movers_commands.handle_gainers_losers(chat_id, ["/losers"], "losers")
        return True
    if any(word in low for word in ("movers", "movement", "stock movement", "market movement")):
        movers_commands.handle_movers(chat_id, ["/movers"])
        return True
    if "news" in low and any(
        word in low for word in ("stock", "latest", "market", "watchlist", "list",
                           "hold", "holding", "share", "company")
    ):
        watchlist_commands.handle_news(chat_id, ["/news"])
        return True
    if "increase" in low and ("share" in low or "holder" in low):
        descriptor = {"mode": "types", "types": list(INCREASE_TYPES)}
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
    corporate_action_commands.run_ca_query(chat_id, descriptor)
    return True


def handle_callback_query(callback) -> None:
    """Handle an inline-button tap.

    Supported buttons:
      stknext:<deep>:<start> - the 'Next' pagination button on stock batches
      fund:<SYMBOL>          - symbol button -> /fundamentalreport SYMBOL
      ana:<SYMBOL>           - symbol button -> /fundamentalanalyze SYMBOL
      mfund                  - 'Get Fundamentals' on a movers report

    Answers the callback first so Telegram clears the loading spinner.
    """
    data = (callback.get("data") or "").strip()
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    callback_id = callback.get("id")
    if not data or chat_id is None:
        return
    answer_callback_query(callback_id)

    if data == "mfund":
        # "Get Fundamentals" button on a movers report: enrich the last
        # screen this chat ran (stored at send time) and send the full
        # fundamentals report. If the stored screen is gone (bot restarted),
        # tell the user to re-run the movers command.
        context = movers_commands._LAST_SCREEN.get(chat_id)
        if not context:
            reply(chat_id, "The movers screen expired - please re-run your movers command, then tap again.")
            return
        log.info("callback mfund for chat %s (%d symbols)", chat_id, len(context["rows"]))
        movers_commands.send_screen_fundamentals(
            chat_id,
            context["rows"], context["header"], context["failed"], context["symbols"],
            "mfund", monotonic(), is_us=bool(context.get("us")),
        )
        return

    if data.startswith("fund:") or data.startswith("ana:"):
        # Symbol cross-link buttons: tap a ticker in any report to open its
        # fundamentals immediately (deep /fundamentalreport or the quick card).
        symbol = data.split(":", 1)[1].strip().upper()
        if not symbol:
            return
        log.info("callback %s for symbol %s (chat %s)", data.split(":", 1)[0], symbol, chat_id)
        if data.startswith("fund:"):
            fundamentals_commands.handle_fund_analysis(chat_id, ["/fundamentalreport", symbol])
        else:
            fundamentals_commands.handle_single_stock_analysis(chat_id, ["/fundamentalanalyze", symbol])
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
    command = "/fundamentalreport" if deep else "/fundamentalanalyze"
    lines, next_start = fundamentals_commands.build_stock_batch(chat_id, command, (start, None), deep)
    markup = fundamentals_commands.stock_next_markup(deep, next_start) if next_start else None
    reply_messages(chat_id, split_messages(lines), reply_markup=markup)
