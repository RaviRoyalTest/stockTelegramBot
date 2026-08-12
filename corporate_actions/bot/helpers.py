"""Small helpers shared by several command families."""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..core.text import escape
from ..sources import get_nse_stock_list_cached, get_quote, search_stocks
from .reply import reply

log = logging.getLogger(__name__)

MAX_QUERY_ITEMS = 20  # entries per message batch
MAX_NEWS_STOCKS = 10  # stocks processed by /news per request
MAX_STOCK_BATCH = 10
MAX_FUND_BATCH = 5


def run_command_sequence(chat_id, commands: list[str], intro: str,
                         done: str | None = None, source_note: str | None = None) -> None:
    """Run a list of commands with a labelled intro and per-command isolation.

    Shared by /myfavourites run and /schedule run (and /schednow): sends the
    intro + the commands being run, executes each one (a failing command must
    never stop the rest), and optionally closes with a completion line. The
    source_note names the watchlist the results relate to, so automatic runs
    are never anonymous.
    """
    if source_note:
        intro += f"\nSource list: <code>{html.escape(source_note)}</code>"
    reply(
        chat_id,
        intro + "\n" + "\n".join(
            f"  \u2022 <code>{html.escape(command)}</code>" for command in commands
        ),
    )
    for command in commands:
        try:
            log.info("run sequence: executing %s (chat %s)", command, chat_id)
            from .dispatch import handle_command  # late import: breaks the module cycle
            handle_command(chat_id, command)
        except Exception as error:
            log.warning(
                "run sequence: command %s failed: %s",
                command, config.redact(error), exc_info=True,
            )
            try:
                reply(
                    chat_id,
                    f"<code>{html.escape(command)}</code> failed: "
                    f"{html.escape(config.redact(str(error)))}",
                )
            except Exception:
                pass
    if done:
        reply(chat_id, done)


def attach_quotes(actions: list[dict], max_workers: int = 6) -> None:
    """Fetch current prices for the actions in parallel and attach them."""
    if not actions:
        return
    for action in actions:
        action.pop("quote", None)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(
            executor.map(
                lambda action: (action, get_quote(action["exchange"], action["symbol"])),
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


def close_symbols(query: str, limit: int = 3) -> list[str]:
    """Fuzzy NSE symbol suggestions via difflib (e.g. 'gensys' -> GENESYS).

    Exact substring search fails on typos and symbol-vs-company-name
    mismatches, so fall back to close matches from the full NSE list.
    """
    try:
        from difflib import get_close_matches

        stocks = get_nse_stock_list_cached()
        symbols = [stock["symbol"] for stock in stocks]
    except Exception:
        return []
    return get_close_matches((query or "").upper(), symbols, n=limit, cutoff=0.72)


def reply_suggestions(chat_id, query, command="add"):
    """Suggest matching NSE stocks when an exact symbol fails (ticker + name).

    Shows up to 6 close matches as a pick list (symbol — full company name),
    then a 'Try:' line reusing the command the user actually ran
    (add|fundamentalanalyze|fundamentalreport|harmonic|checklist) so a tap
    runs the right follow-up.
    """
    matches = search_stocks(query, limit=10)
    if not matches:
        log.info(
            "No stock matched '%s' for chat %s - nothing added", query, chat_id
        )
        reply(
            chat_id,
            f"No NSE stock matches '<code>{escape(query)}</code>' — check the "
            "spelling or try a company name (e.g. <code>/fundamentalanalyze RELIANCE</code>).",
        )
        return
    lines = [f"'{escape(query)}' isn't an exact NSE symbol. Did you mean one of these?"]
    for match in matches[:6]:
        company = escape(match.get("company") or "")
        lines.append(f"• <code>{escape(match['symbol'])}</code> — {company} (NSE)")
    lines.append("")
    if command == "add":
        lines.append(f"Try: <code>/addstock {escape(matches[0]['symbol'])} NSE</code>")
    else:
        lines.append(f"Try: <code>/{command} {escape(matches[0]['symbol'])}</code>")
    reply(chat_id, "\n".join(lines))
