"""Small helpers shared by several command families."""
from __future__ import annotations

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


def attach_quotes(actions: list[dict], max_workers: int = 6) -> None:
    """Fetch current prices for the actions in parallel and attach them."""
    if not actions:
        return
    for a in actions:
        a.pop("quote", None)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(
            ex.map(
                lambda a: (a, get_quote(a["exchange"], a["symbol"])),
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
        symbols = [s["symbol"] for s in stocks]
    except Exception:
        return []
    return get_close_matches((query or "").upper(), symbols, n=limit, cutoff=0.72)


def reply_suggestions(chat_id, query, cmd="add"):
    """Reply with matching stocks from the NSE list when an exact symbol fails.

    cmd is the command the user actually ran (add|stock|fund|harmonic) so the
    suggested follow-up reuses it instead of always suggesting /add.
    """
    matches = search_stocks(query, limit=10)
    if not matches:
        log.info(
            "No stock matched '%s' for chat %s - nothing added", query, chat_id
        )
        reply(chat_id, f"No stocks match '{query}'.")
        return
    lines = [f"'{escape(query)}' not found as an exact symbol. Did you mean (NSE):"]
    for m in matches:
        company = m["company"] or ""
        if cmd == "add":
            lines.append(f"  /addstock {m['symbol']} NSE  - {escape(company)}")
        else:
            lines.append(f"  /{cmd} {m['symbol']}  - {escape(company)}")
    reply(chat_id, "\n".join(lines))
