"""Watchlist commands: /watchlist, /addstock, /removestock, favourites, /news."""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor

from .. import config, storage
from ..formatting import format_news_list, format_next_report
from ..github import github_push_configured
from ..poller.events import parse_ex_date, recently_passed, within_reminder_window
from ..poller.fetchers import fetch_matching
from ..sources import (
    get_bse_stock_list,
    get_quote,
    get_stock_news,
    search_stocks,
)
from ..telegram.client import NotifierError, send_message
from ..telegram.markup import symbol_buttons
from .helpers import MAX_NEWS_STOCKS, attach_quotes, reply_suggestions
from .reply import reply

log = logging.getLogger(__name__)

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
    except Exception as exc:
        reply(chat_id, f"Could not fetch corporate actions: {html.escape(config.redact(str(exc)))}")
        return
    upcoming = [
        a for a in matching if within_reminder_window(a.get("ex_date"))
    ]
    recent = [
        a for a in matching if recently_passed(a.get("ex_date"))
    ]
    pending = [
        a for a in matching if not parse_ex_date(a.get("ex_date"))
    ]
    # Attach live prices so each colorful block can show the current price.
    for group in (upcoming, recent, pending):
        attach_quotes(group)
    # Cross-link: one tappable button per symbol -> deep fundamentals
    seen, tap_symbols = set(), []
    for a in upcoming + recent + pending:
        sym = (a.get("symbol") or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            tap_symbols.append(sym)
    reply(
        chat_id,
        format_next_report(upcoming, recent, pending),
        reply_markup=symbol_buttons(tap_symbols[:12], "fund") if tap_symbols else None,
    )


def handle_add_remove(chat_id, parts, cmd) -> None:
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
        exchange = exchange if exchange in ("NSE", "BSE") else "NSE"

    if cmd in ("/add", "/addstock"):
        quote = get_quote(exchange, symbol)
        company = quote.get("name", "") if quote else ""
        validated = quote is not None
        if not validated and exchange == "NSE":
            exact = next(
                (
                    s for s in search_stocks(symbol, limit=5)
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
                bse_list = get_bse_stock_list()
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
            reply_suggestions(chat_id, symbol)
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


# --------------------------------------------------------------- favourites
def _get_favourites(chat_id) -> list[str]:
    """Return the chat's favourite-command list (defaults when not set)."""
    cmds = storage.get_user_settings(chat_id).get("favourites")
    if isinstance(cmds, list) and cmds:
        return [str(c).strip() for c in cmds if str(c).strip()]
    return list(DEFAULT_FAVOURITES)


def _save_favourites(chat_id, cmds: list[str]) -> None:
    settings = storage.get_user_settings(chat_id)
    settings["favourites"] = cmds
    storage.save_user_settings(chat_id, settings)


def _favourites_summary(chat_id) -> str:
    cmds = _get_favourites(chat_id)
    lines = ["\U0001F4CB <b>Your Favourites</b> - these run when you type "
             "<code>/myfavourites run</code>:\n"]
    for i, c in enumerate(cmds, 1):
        lines.append(f"  {i}. <code>{html.escape(c)}</code>")
    lines.append(
        "\nChange them: <code>/myfavourites set /cmd</code>, "
        "<code>/myfavourites add /cmd</code>, "
        "<code>/myfavourites remove N</code>, <code>/myfavourites reset</code>."
    )
    return "\n".join(lines)


def _run_favourites(chat_id) -> None:
    cmds = _get_favourites(chat_id)
    reply(
        chat_id,
        "\U0001F4CB <b>Your Favourites</b> - running your commands...\n"
        + "\n".join(f"  \u2022 <code>{html.escape(c)}</code>" for c in cmds),
    )
    for cmd in cmds:
        try:
            log.info("favourites: running %s (chat %s)", cmd, chat_id)
            from .dispatch import handle_command  # late import: breaks the module cycle
            handle_command(chat_id, cmd)
        except Exception as exc:
            log.warning("favourites: command %s failed: %s", cmd, config.redact(exc), exc_info=True)
            try:
                reply(chat_id, f"<code>{html.escape(cmd)}</code> failed: {html.escape(config.redact(str(exc)))}")
            except Exception:
                pass
    reply(
        chat_id,
        "\u2705 <b>Favourites done.</b> Run again with <code>/myfavourites run</code> "
        "or edit the list with <code>/myfavourites set /cmd</code>.",
    )


def _group_favourite_cmds(tokens: list[str]) -> list[str]:
    """Group raw tokens into commands, starting a new command at each '/' token.

    /myfavourites set /toplosers 1h /news -> ['/toplosers 1h', '/news']
    """
    cmds: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("/") or not cmds:
            cmds.append(tok)
        else:
            cmds[-1] += " " + tok
    return [c for c in cmds if c.startswith("/")]


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
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("run", "now", "execute"):
        _run_favourites(chat_id)
        return

    if sub == "set":
        cmds = _group_favourite_cmds(parts[2:])
        if not cmds:
            reply(chat_id, "Usage: <code>/myfavourites set /cmd1 /cmd2 ...</code>")
            return
        _save_favourites(chat_id, cmds)
        log.info("chat %s set favourites: %s", chat_id, cmds)
        reply(chat_id, f"\u2705 Favourites updated.\n\n{_favourites_summary(chat_id)}")
        return

    if sub == "add":
        cmds = _get_favourites(chat_id)
        added = _group_favourite_cmds(parts[2:])
        if not added:
            reply(chat_id, "Usage: <code>/myfavourites add /cmd</code>")
            return
        cmds.extend(added)
        _save_favourites(chat_id, cmds)
        log.info("chat %s added favourites: %s", chat_id, added)
        reply(chat_id, f"\u2705 Favourites updated.\n\n{_favourites_summary(chat_id)}")
        return

    if sub == "remove":
        cmds = _get_favourites(chat_id)
        try:
            n = int(parts[2]) - 1 if len(parts) > 2 else -1
        except ValueError:
            n = -1
        if n < 0 or n >= len(cmds):
            reply(chat_id, "Usage: <code>/myfavourites remove N</code> (number from the list).")
            return
        removed = cmds.pop(n)
        _save_favourites(chat_id, cmds)
        log.info("chat %s removed favourite %s", chat_id, removed)
        reply(chat_id, f"\u2705 Removed <code>{html.escape(removed)}</code>.\n\n{_favourites_summary(chat_id)}")
        return

    if sub == "reset":
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
        except Exception as exc:  # a failing stock must not break the batch
            log.info("news fetch failed for %s: %s", item.get("symbol"), exc)
            return item, []

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(_fetch, items))
    log.info("news: fetched headlines for %d/%d stock(s)", len(fetched), len(items))

    for item, news in fetched:
        try:
            send_message(
                format_news_list(item["symbol"], item["exchange"], news),
                chat_id=chat_id,
            )
        except NotifierError as exc:
            log.warning("news reply failed for chat %s: %s", chat_id, exc)
            return
    if truncated:
        reply(chat_id, f"(showing news for the first {MAX_NEWS_STOCKS} stocks)")
