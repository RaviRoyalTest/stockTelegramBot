"""Watchlist commands: /watchlist, /addstock, /removestock, favourites, /news."""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor

from .. import config, storage
from ..formatting import format_news_list, format_next_report
from ..github import github_push_configured
from ..poller.events import (
    action_is_completed,
    parse_ex_date,
    recently_passed,
    within_reminder_window,
)
from ..poller.fetchers import fetch_matching
from ..sources import (
    get_bse_stock_list,
    get_quote,
    get_stock_news,
    search_stocks,
    search_us_tickers,
)
from ..telegram.client import NotifierError, send_message
from ..telegram.markup import symbol_buttons
from .helpers import (
    MAX_NEWS_STOCKS,
    attach_quotes,
    reply_suggestions,
    resolve_or_none,
    run_command_sequence,
)
from .reply import reply

log = logging.getLogger(__name__)

BULK_ADD_LIMIT = 60  # max symbols per bulk /add or /setlist message

DEFAULT_FAVOURITES = [
    "/corpactionsformylist",
    "/toplosers 1h",
    "/toplosers 1d 10",
    "/watchlist",
    "/fundamentalreport mylist",
]


def send_watchlist(chat_id) -> None:
    """Show the requester's full watchlist (/watchlist, /list)."""
    items = storage.get_user_list(chat_id)
    if not items:
        reply(chat_id, "Your watchlist is empty.")
        return
    lines = [
        f"{index}. <b>{item['symbol']}</b> ({item['exchange']})"
        for index, item in enumerate(items, start=1)
    ]
    where = storage.list_location(chat_id)
    persistence = (
        "pushed to GitHub - it survives redeploys."
        if github_push_configured()
        else "NOT pushed to GitHub - it is only on this host's disk "
        "and WILL BE LOST on redeploy. Run /status to confirm."
    )
    # Cross-link: tap a ticker below to open its fundamentals immediately
    tap_symbols = [item["symbol"] for item in items[:12]]
    reply(
        chat_id,
        "<b>Your Watchlist:</b>\n"
        + "\n".join(lines)
        + f"\n\nUse <code>/fundamentalanalyze 5-10</code> or <code>/fundamentalreport 3-5</code> to get details by these numbers."
        + "\nTap a ticker below for its fundamentals."
        + f"\nSaved in: <code>{html.escape(where)}</code>\nPersistence: {html.escape(persistence)}",
        reply_markup=symbol_buttons(tap_symbols, "fund") if tap_symbols else None,
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
        matching = fetch_matching(items)
    except Exception as error:
        reply(chat_id, f"Could not fetch corporate actions: {html.escape(config.redact(str(error)))}")
        return
    upcoming = [
        action for action in matching if within_reminder_window(action.get("ex_date"))
    ]
    recent = [
        action
        for action in matching
        if recently_passed(action.get("ex_date"))
        and not action_is_completed(action)
    ]
    pending = [
        action for action in matching if not parse_ex_date(action.get("ex_date"))
    ]
    # Attach live prices so each colorful block can show the current price.
    for group in (upcoming, recent, pending):
        attach_quotes(group)
    # Cross-link: one tappable button per symbol -> deep fundamentals
    seen, tap_symbols = set(), []
    for action in upcoming + recent + pending:
        symbol = (action.get("symbol") or "").upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            tap_symbols.append(symbol)
    reply(
        chat_id,
        format_next_report(upcoming, recent, pending),
        reply_markup=symbol_buttons(tap_symbols[:12], "fund") if tap_symbols else None,
    )


def _parse_symbol_args(text: str) -> list[str]:
    """Split a bulk symbol string on commas and/or whitespace, uppercase, deduped."""
    raw = (text or "").replace("\n", ",")
    tokens = [tok.strip().upper() for seg in raw.split(",") for tok in seg.split()]
    seen, ordered = set(), []
    for tok in tokens:
        if tok and tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered


def handle_bulk_add(chat_id, text) -> None:
    """Add many stocks at once: /add RELIANCE, INFY, CANBK (missing only).

    Every symbol is validated; unknown ones are reported with suggestions,
    duplicates are skipped, and the existing list is never trimmed.
    """
    tokens = _parse_symbol_args(text)
    if not tokens:
        reply(chat_id, "Usage: <code>/add RELIANCE, INFY, CANBK</code> (comma or space separated).")
        return
    if len(tokens) == 1:
        handle_add_remove(chat_id, ["/addstock", tokens[0]], "/addstock")
        return
    where = storage.list_location(chat_id)
    added: list[str] = []
    skipped: list[str] = []
    unknown: list[str] = []
    for token in tokens[:BULK_ADD_LIMIT]:
        resolved = resolve_or_none(token)
        if resolved is None:
            unknown.append(token)
            continue
        before = len(storage.get_user_list(chat_id))
        storage.add_to_user_list(chat_id, resolved)
        if len(storage.get_user_list(chat_id)) > before:
            added.append(resolved["symbol"])
        else:
            skipped.append(resolved["symbol"])
    lines = [f"<b>Bulk add</b> - {len(tokens)} symbol(s) checked:"
             f"\n\u2705 Added: <b>{len(added)}</b>"
             f"\n\u23ED\uFE0F Already in list: <b>{len(skipped)}</b>"
             f"\n\u274C Not found: <b>{len(unknown)}</b>"]
    if added:
        lines.append("\nNew: " + ", ".join(added))
    if skipped:
        lines.append("\nPresent already: " + ", ".join(skipped))
    if unknown:
        lines.append("\nUnknown (check spelling): " + ", ".join(unknown))
    lines.append(f"\nSaved in: <code>{html.escape(where)}</code>")
    reply(chat_id, "\n".join(lines))


def handle_setlist(chat_id, parts) -> None:
    """Make the watchlist EXACTLY the given stocks: /setlist A, B, C ...

    Duplicates in the input collapse to one; stocks already present stay
    exactly where they are; the final list follows the order you typed.
    Prefix the list with 'check' to preview without changing anything.
    """
    raw = " ".join(parts[1:]).strip()
    preview = False
    if raw.lower().startswith("check "):
        preview = True
        raw = raw[6:].strip()
    tokens = _parse_symbol_args(raw)
    if not tokens:
        reply(
            chat_id,
            "Usage: <code>/setlist RELIANCE, INFY, CANBK</code>\n"
            "Makes your watchlist <b>exactly</b> these stocks (duplicates ignored, "
            "everything else removed).\n"
            "Preview first with <code>/setlist check RELIANCE, INFY</code>.",
        )
        return
    if len(tokens) > BULK_ADD_LIMIT:
        reply(chat_id, f"That is {len(tokens)} symbols - the limit is <b>{BULK_ADD_LIMIT}</b> per /setlist.")
        return
    resolved: list[dict] = []
    unknown: list[str] = []
    for token in tokens:
        item = resolve_or_none(token)
        if item is None:
            unknown.append(token)
        else:
            resolved.append(item)
    if unknown:
        lines = ["\u26A0\uFE0F <b>Nothing was changed</b> - these could not be validated:"]
        lines.append(", ".join(unknown))
        lines.append("Fix or remove them, then run /setlist again. (Everything must be valid so a typo never wipes stocks you wanted to keep.)")
        reply(chat_id, "\n".join(lines))
        return
    wanted = [f"<b>{i['symbol']}</b> ({i['exchange']})" for i in resolved]
    if preview:
        reply(
            chat_id,
            "<b>Preview only</b> - nothing changed.\n"
            f"Your watchlist would become exactly <b>{len(resolved)}</b> stock(s):\n"
            + "\n".join(f"{n}. {s}" for n, s in enumerate(wanted, 1))
            + "\n\nRun it for real: <code>/setlist " + ", ".join(tokens[:20])
            + ("..." if len(tokens) > 20 else "") + "</code>",
        )
        return
    before_syms = {str(i.get("symbol", "")).upper() for i in storage.get_user_list(chat_id)}
    result = storage.set_user_list_exact(chat_id, resolved)
    new_list = result["list"]
    new_syms = {str(i.get("symbol", "")).upper() for i in new_list}
    removed = sorted(before_syms - new_syms)
    kept = sorted(before_syms & new_syms)
    lines = [
        "\U0001F4CB <b>Watchlist set</b> - exact list mode:",
        f"\u2705 Now tracking <b>{len(new_list)}</b> stock(s) (you asked for {len(tokens)}).",
        f"\u21BB Kept (already present): <b>{len(kept)}</b>",
        f"\u2795 Newly added: <b>{len(new_syms - before_syms)}</b>",
        f"\u2796 Removed (not in your list): <b>{len(removed)}</b>",
    ]
    if removed:
        lines.append("\nRemoved: " + ", ".join(removed))
    lines.append("\nUse <code>/watchlist</code> to see the final list.")
    reply(chat_id, "\n".join(lines))


def handle_add_remove(chat_id, parts, command) -> None:
    """Add or remove a stock for the requester (/add, /addstock, /remove, /removestock)."""
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
        exchange = exchange if exchange in ("NSE", "BSE", "US") else "NSE"

    if command in ("/add", "/addstock"):
        quote = get_quote(exchange, symbol)
        company = quote.get("name", "") if quote else ""
        validated = quote is not None
        if not validated and exchange == "NSE":
            exact = next(
                (
                    stock for stock in search_stocks(symbol, limit=5)
                    if stock["symbol"].upper() == symbol
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
                bse_list = get_bse_stock_list()
                exact = next(
                    (stock for stock in bse_list if stock["symbol"].upper() == symbol or stock.get("code") == symbol),
                    None,
                )
                if exact is not None:
                    symbol = exact["symbol"]
                    company = exact.get("company", "")
                    validated = True
            except Exception:
                pass
        elif not validated and exchange == "US":
            try:
                us_matches = search_us_tickers(symbol, limit=1)
                if us_matches:
                    exact = us_matches[0]
                    symbol = exact.get("symbol", symbol)
                    company = exact.get("name") or exact.get("company", "")
                    validated = True
            except Exception:
                pass

        if not validated:
            reply_suggestions(chat_id, symbol)
            return
        storage.add_to_user_list(
            chat_id,
            {"symbol": symbol, "company": company, "exchange": exchange},
        )
        where = storage.list_location(chat_id)
        log.info("Added %s (%s) for chat %s -> %s", symbol, exchange, chat_id, where)
        reply(
            chat_id,
            f"Added <b>{symbol}</b> ({exchange}). Alerts will come to this chat.\n"
            f"Saved in: <code>{html.escape(where)}</code>.",
        )
    elif command in ("/remove", "/removestock"):
        storage.remove_from_user_list(chat_id, symbol, exchange)
        log.info("Removed %s (%s) for chat %s", symbol, exchange, chat_id)
        reply(chat_id, f"Removed <b>{symbol}</b> ({exchange}) if it was present.")


# --------------------------------------------------------------- favourites
def _get_favourites(chat_id) -> list[str]:
    """Return the chat's favourite-command list (defaults when not set)."""
    commands = storage.get_user_settings(chat_id).get("favourites")
    if isinstance(commands, list) and commands:
        return [str(command).strip() for command in commands if str(command).strip()]
    return list(DEFAULT_FAVOURITES)


def _save_favourites(chat_id, commands: list[str]) -> None:
    settings = storage.get_user_settings(chat_id)
    settings["favourites"] = commands
    storage.save_user_settings(chat_id, settings)


def _favourites_summary(chat_id) -> str:
    commands = _get_favourites(chat_id)
    lines = ["\U0001F4CB <b>Your Favourites</b> - these run when you type "
             "<code>/myfavourites run</code>:\n"]
    for index, command in enumerate(commands, 1):
        lines.append(f"  {index}. <code>{html.escape(command)}</code>")
    lines.append(
        "\nChange them: <code>/myfavourites set /toplosers 1h /news</code>, "
        "<code>/myfavourites add /watchlist</code>, "
        "<code>/myfavourites remove N</code>, <code>/myfavourites reset</code>."
    )
    return "\n".join(lines)


def _run_favourites(chat_id) -> None:
    commands = _get_favourites(chat_id)
    run_command_sequence(
        chat_id,
        commands,
        intro="\U0001F4CB <b>Your Favourites</b> - running your commands...",
        done="\u2705 <b>Favourites done.</b> Run again with "
             "<code>/myfavourites run</code> or edit the list with "
             "<code>/myfavourites set /toplosers 1h /news</code>.",
        source_note=storage.list_location(chat_id),
    )


def _group_favourite_commands(tokens: list[str]) -> list[str]:
    """Group raw tokens into commands, starting a new command at each '/' token.

    /myfavourites set /toplosers 1h /news -> ['/toplosers 1h', '/news']
    '//' typos collapse to the single-slash command they clearly were meant
    to be (dispatch.py normalizes the message command; this catches tokens).
    """
    commands: list[str] = []
    for token in tokens:
        if not token:
            continue
        if token.startswith("//"):
            token = token[1:]
        if token.startswith("/") or not commands:
            commands.append(token)
        else:
            commands[-1] += " " + token
    return [command for command in commands if command.startswith("/")]


# Tokens commonly pasted from the help text instead of a real command.
_PLACEHOLDER_COMMANDS = {"/cmd", "/command", "/c1", "/c2", "/example"}
# Commands that would run favourites again - never allowed on the list.
_SELF_REFERENCE_COMMANDS = {
    "/myfavourites", "/favorites", "/favourites", "/mypicks", "/dailybrief",
}


def _validate_favourite_commands(commands: list[str]) -> tuple[list[str], list[str]]:
    """Split favourite candidates into (valid, problems).

    Each problem is a human-readable line: unknown command, placeholder
    ('/cmd' is the help-text example, not a command), a favourites command
    (would run forever inside /myfavourites run) or an empty token.
    """
    from .registry import is_known_command, normalize_command

    valid: list[str] = []
    problems: list[str] = []
    for command in commands:
        first = command.strip().split()[0] if command.strip() else ""
        canonical = normalize_command(first) if first else None
        if not first:
            problems.append("(empty command)")
            continue
        if first.lower() in _PLACEHOLDER_COMMANDS:
            problems.append(
                f"<code>{html.escape(command)}</code> - <code>{first}</code> is the "
                "help-text placeholder. Type the real command, e.g. "
                "<code>/myfavourites set /toplosers 1h /news</code>."
            )
            continue
        if canonical in _SELF_REFERENCE_COMMANDS:
            problems.append(
                f"<code>{html.escape(command)}</code> - favourites cannot contain "
                "themselves (<code>/myfavourites run</code> would never stop)."
            )
            continue
        if not is_known_command(command):
            problems.append(
                f"<code>{html.escape(command)}</code> - unknown command. "
                "Check the spelling with /help."
            )
            continue
        valid.append(command)
    return valid, problems


def handle_favourites(chat_id, parts=None) -> None:
    """Show / run / edit the user's favourite commands (/myfavourites).

    /myfavourites          -> show the current list (and how to edit it)
    /myfavourites run      -> run the list now
    /myfavourites set /c1 /c2 ... -> replace the whole list
    /myfavourites add /c   -> append a command
    /myfavourites remove N -> drop command #N (1-based)
    /myfavourites reset    -> restore the default list
    """
    parts = parts or ["/myfavourites"]
    subcommand = parts[1].lower() if len(parts) > 1 else ""

    if subcommand in ("run", "now", "execute"):
        _run_favourites(chat_id)
        return

    if subcommand == "set":
        commands = _group_favourite_commands(parts[2:])
        if not commands:
            reply(chat_id, "Usage: <code>/myfavourites set /cmd1 /cmd2 ...</code>")
            return
        commands, problems = _validate_favourite_commands(commands)
        if not commands:
            reply(
                chat_id,
                "\u26A0\uFE0F <b>Nothing was saved</b> - none of those are runnable commands:\n"
                + "\n".join(problems)
                + "\n\nExample: <code>/myfavourites set /toplosers 1h /news</code>",
            )
            return
        _save_favourites(chat_id, commands)
        log.info("chat %s set favourites: %s", chat_id, commands)
        notice = "\u2705 Favourites updated."
        if problems:
            notice += "\n\u26A0\uFE0F Skipped:\n" + "\n".join(problems)
        reply(chat_id, notice + f"\n\n{_favourites_summary(chat_id)}")
        return

    if subcommand == "add":
        commands = _get_favourites(chat_id)
        added = _group_favourite_commands(parts[2:])
        if not added:
            reply(chat_id, "Usage: <code>/myfavourites add /cmd</code>")
            return
        added, problems = _validate_favourite_commands(added)
        existing_keys = {c.strip().lower() for c in commands}
        added = [c for c in added if c.strip().lower() not in existing_keys]
        if not added and problems:
            reply(
                chat_id,
                "\u26A0\uFE0F <b>Nothing was added</b>:\n" + "\n".join(problems),
            )
            return
        commands.extend(added)
        _save_favourites(chat_id, commands)
        log.info("chat %s added favourites: %s", chat_id, added)
        notice = "\u2705 Favourites updated."
        if problems:
            notice += "\n\u26A0\uFE0F Skipped:\n" + "\n".join(problems)
        reply(chat_id, notice + f"\n\n{_favourites_summary(chat_id)}")
        return

    if subcommand == "remove":
        commands = _get_favourites(chat_id)
        try:
            position = int(parts[2]) - 1 if len(parts) > 2 else -1
        except ValueError:
            position = -1
        if position < 0 or position >= len(commands):
            reply(chat_id, "Usage: <code>/myfavourites remove N</code> (number from the list).")
            return
        removed = commands.pop(position)
        _save_favourites(chat_id, commands)
        log.info("chat %s removed favourite %s", chat_id, removed)
        reply(chat_id, f"\u2705 Removed <code>{html.escape(removed)}</code>.\n\n{_favourites_summary(chat_id)}")
        return

    if subcommand == "reset":
        _save_favourites(chat_id, list(DEFAULT_FAVOURITES))
        log.info("chat %s reset favourites to defaults", chat_id)
        reply(chat_id, f"\u2705 Favourites reset to defaults.\n\n{_favourites_summary(chat_id)}")
        return

    reply(chat_id, _favourites_summary(chat_id))


# -------------------------------------------------------------------- news
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
            news = get_stock_news(item["exchange"], item["symbol"], per_stock)
            return item, news
        except Exception as error:  # a failing stock must not break the batch
            log.info("news fetch failed for %s: %s", item.get("symbol"), error)
            return item, []

    with ThreadPoolExecutor(max_workers=6) as executor:
        fetched = list(executor.map(_fetch, items))
    log.info("news: fetched headlines for %d/%d stock(s)", len(fetched), len(items))

    for item, news in fetched:
        try:
            send_message(
                format_news_list(item["symbol"], item["exchange"], news),
                chat_id=chat_id,
            )
        except NotifierError as error:
            log.warning("news reply failed for chat %s: %s", chat_id, error)
            return
    if truncated:
        reply(chat_id, f"(showing news for the first {MAX_NEWS_STOCKS} stocks)")
