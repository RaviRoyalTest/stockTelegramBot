"""Investment-checklist command: /checklist (aliases /investcheck, /scorecard).

Checks a stock against the 32-point checklist (10 personal + 22 AI criteria)
and returns a scored pass/fail card. Supports one symbol or watchlist
position ranges like the other stock commands:
  /checklist RELIANCE   -> scorecard for one stock
  /checklist mylist     -> your whole watchlist (3 per batch)
  /checklist 5-10       -> watchlist positions #5..#10
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .. import storage
from ..core.text import escape, split_messages
from ..formatting.checklist import format_checklist
from ..sources import get_fundamentals, get_quote, get_us_fundamentals
from .fundamentals_commands import parse_stock_range
from .helpers import reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

CHECKLIST_BATCH = 3  # full checklists are long - cap a batch request

CHECKLIST_HELP = (
    "<b>/checklist</b> - 32-point investment scorecard "
    "(10 personal + 22 AI criteria)\n"
    "/checklist RELIANCE  \u2192 scorecard for one stock\n"
    "/checklist mylist    \u2192 your whole watchlist (3 per batch)\n"
    "/checklist 5-10      \u2192 watchlist positions #5-#10\n"
    "/checklist 1         \u2192 your first stock"
)


def _fetch_stock(symbol: str, exchange: str = "") -> tuple[dict, dict, str]:
    """Quote + deep fundamentals for one symbol (best-effort, parallel-safe).

    Auto-detects the market NSE -> BSE -> US like the other stock commands,
    unless a watchlist exchange is given; returns (quote, fund, currency)
    where currency is 'INR' or 'USD'.
    """
    exchange = (exchange or "").upper()
    quote = get_quote(exchange, symbol) if exchange in ("NSE", "BSE", "US") else \
        (get_quote("NSE", symbol) or get_quote("BSE", symbol) or {})
    currency = "INR"
    if exchange != "US" and quote.get("price") is None:
        us_quote = get_quote("US", symbol) or {}
        if us_quote.get("price") is not None or us_quote.get("name"):
            quote = us_quote
            currency = "USD"
    elif exchange == "US":
        currency = "USD"
    if currency == "USD":
        fund = get_us_fundamentals(symbol) or {}
    else:
        fund = get_fundamentals(symbol, with_screener=True) or {}
    return quote, fund, currency


def handle_single_checklist(chat_id, raw_symbol: str) -> None:
    """Render the full checklist card for one symbol."""
    symbol = raw_symbol.upper().strip().removesuffix(".NS").removesuffix(".BO")
    log.info("checklist: scoring %s (chat %s)", symbol, chat_id)
    quote, fund, currency = _fetch_stock(symbol)
    if quote.get("price") is None and not fund:
        reply_suggestions(chat_id, symbol, "checklist")
        return
    lines = format_checklist(symbol, quote, fund, currency=currency)
    reply_messages(chat_id, split_messages(lines))


def handle_checklist_batch(chat_id, range) -> None:
    """Score a range of watchlist positions (3 per batch)."""
    items = storage.get_user_list(chat_id)
    if not items:
        reply(chat_id, "Your watchlist is empty. Add stocks with /addstock SYMBOL NSE")
        return
    start, end = range
    total = len(items)
    start = max(1, start)
    end = total if end is None else min(end, total)
    if start > total:
        reply(
            chat_id,
            f"Your watchlist has only {total} stock(s) - start position must be 1..{total}.",
        )
        return
    work = items[start - 1:end][:CHECKLIST_BATCH]
    log.info(
        "checklist batch: positions %d-%d (%d stocks, chat %s)",
        start, start + len(work) - 1, len(work), chat_id,
    )

    with ThreadPoolExecutor(max_workers=max(1, min(6, len(work)))) as executor:
        results = list(executor.map(
            lambda item: (item, *_fetch_stock(item["symbol"], item.get("exchange", ""))), work,
        ))

    all_lines: list[str] = []
    for index, (item, quote, fund, currency) in enumerate(results, start=start):
        label = f"<b>#{index}</b>"
        if quote.get("price") is None and not fund:
            all_lines.append(
                f"\U0001F4DD {label} <b>{escape(item['symbol'])}</b> \u2014 "
                "no data available right now."
            )
            all_lines.append("")
            continue
        lines = format_checklist(item["symbol"], quote, fund, currency=currency)
        lines[0] = f"{label} " + lines[0]
        all_lines.extend(lines)
        all_lines.append("")
        all_lines.append("\u2501" * 24)
        all_lines.append("")

    if start + len(work) - 1 < total:
        all_lines.append(
            f"\u2026 and {total - (start + len(work) - 1)} more. Send "
            f"<code>/checklist {start + len(work)}-{total}</code> for the next batch."
        )
    reply_messages(chat_id, split_messages(all_lines))


def handle_checklist(chat_id, parts) -> None:
    """Route /checklist to a single-stock card or a watchlist range."""
    if len(parts) < 2:
        reply(chat_id, CHECKLIST_HELP)
        return
    range = parse_stock_range(parts[1])
    if range is not None:
        handle_checklist_batch(chat_id, range)
        return
    handle_single_checklist(chat_id, parts[1])
