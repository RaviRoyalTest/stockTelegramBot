"""Fundamental-analysis commands: quick card + deep report + watchlist batches.

Single symbols (/fundamentalanalyze TATATECH, /fundamentalreport RELIANCE) and
watchlist-position ranges (/fundamentalanalyze 5-10, /fundamentalreport mylist)
with a 'Next' pagination button.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from .. import storage
from ..core.text import escape, split_messages
from ..formatting.stock_india import _fund_report_lines, _stock_summary_lines
from ..formatting.stock_us import _us_stock_lines
from ..sources import get_fundamentals, get_quote, get_us_fundamentals
from .helpers import MAX_FUND_BATCH, MAX_STOCK_BATCH, reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def parse_stock_range(argument: str):
    """Parse a watchlist position range like '5', '5-10', '5 - 10', 'all'.

    Returns a (start, end) tuple with 1-based inclusive positions (end None =
    to the end of the list), or None when the arg looks like a stock symbol.
    """
    token = (argument or "").strip().lower()
    if not token:
        return None
    if token in ("all", "mylist", "my-list", "my list", "*", "full", "everything"):
        return (1, None)
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
    if match:
        range_start, range_end = int(match.group(1)), int(match.group(2))
        return (min(range_start, range_end), max(range_start, range_end))
    match = re.fullmatch(r"(\d+)\s*-", token)
    if match:
        return (int(match.group(1)), None)
    match = re.fullmatch(r"-\s*(\d+)", token)
    if match:
        return (1, int(match.group(1)))
    if token.isdigit():
        return (1, max(1, int(token)))
    return None


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

    range = parse_stock_range(parts[1])
    if range is not None:
        handle_stock_batch(chat_id, "/fundamentalanalyze", range, deep=False)
        return

    raw_symbol = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    started_at = monotonic()
    log.info("handle_single_stock: fetching full details for %s for chat %s", raw_symbol, chat_id)

    quote = get_quote("NSE", raw_symbol) or get_quote("BSE", raw_symbol) or {}
    is_us = False
    if quote.get("price") is None:
        us_quote = get_quote("US", raw_symbol) or {}
        if us_quote.get("price") is not None or us_quote.get("name"):
            quote = us_quote
            is_us = True
    fund = (get_us_fundamentals(raw_symbol) if is_us
            else get_fundamentals(raw_symbol, with_screener=True)) or {}

    if quote.get("price") is None and not fund:
        reply_suggestions(chat_id, raw_symbol, "fundamentalanalyze")
        return

    if is_us:
        lines = _us_stock_lines(raw_symbol, quote, fund, include_tip=True)
    else:
        lines = _stock_summary_lines(raw_symbol, quote, fund, include_tip=True)
    reply_messages(chat_id, ["\n".join(lines)])
    log.info("handle_single_stock: completed for %s in %.1fs", raw_symbol, monotonic() - started_at)


def stock_next_markup(deep: bool, start: int) -> dict:
    """Inline 'Next' button for a paginated batch (callback_data stknext:deep:start)."""
    return {
        "inline_keyboard": [
            [{"text": "Next \u25b6", "callback_data": f"stknext:{1 if deep else 0}:{start}"}],
        ]
    }


def _batch_fund(item: dict) -> dict:
    """Fetch fundamentals for one watchlist item, honouring its exchange."""
    if item.get("exchange", "").upper() == "US":
        return get_us_fundamentals(item["symbol"]) or {}
    return get_fundamentals(item["symbol"], with_screener=True) or {}


def build_stock_batch(chat_id, command: str, range, deep: bool) -> tuple[list[str], int | None]:
    """Fetch and format a page of watchlist positions (never sends).

    Returns (lines, next_start) where next_start is the 1-based start of the
    next page, or None when this is the last page.
    """
    batch_limit = MAX_FUND_BATCH if deep else MAX_STOCK_BATCH
    items = storage.get_user_list(chat_id)
    if not items:
        return ["Your watchlist is empty. Add stocks with /addstock SYMBOL NSE"], None
    start, end = range
    total = len(items)
    start = max(1, start)
    end = total if end is None else min(end, total)
    if start > total:
        return [
            f"Your watchlist has only {total} stock(s) - start position must be 1..{total}."
        ], None
    work = items[start - 1:end][:batch_limit]
    started_at = monotonic()
    log.info(
        "%s batch: positions %d-%d (%d stocks, deep=%s)",
        command, start, start + len(work) - 1, len(work), deep,
    )

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(work)))) as executor:
        quotes = list(executor.map(lambda item: get_quote(item["exchange"], item["symbol"]) or {}, work))
        funds = list(executor.map(lambda item: _batch_fund(item), work))

    body = []
    for positive_flow, (item, quote, fund) in enumerate(zip(work, quotes, funds), start=start):
        symbol = item["symbol"]
        label = f"<b>#{positive_flow}</b>"
        if item.get("exchange", "").upper() == "US":
            lines = _us_stock_lines(symbol, quote, fund, include_tip=False, label=label)
        elif deep:
            lines = _fund_report_lines(symbol, quote, fund, include_tip=False, label=label)
        else:
            lines = _stock_summary_lines(symbol, quote, fund, include_tip=False, label=label)
        if not quote and not fund:
            lines = [
                f"\U0001F4CA {label} <b>{escape(symbol)}</b>",
                "\u26a0\ufe0f No data available right now.",
            ]
        body.extend(lines)
        body.append("")

    header = (
        f"\U0001F4CA <b>{command.upper()} \u00b7 Watchlist positions "
        f"{start}\u2013{start + len(work) - 1} of {total}</b>\n"
    )
    all_lines = [header] + body
    next_start = start + len(work) if start + len(work) <= total else None
    if next_start is not None:
        remaining = total - (start + len(work) - 1)
        page_end = min(total, next_start + batch_limit - 1)
        all_lines.append(
            f"\u2026 and {remaining} more. Tap <b>Next \u25b6</b> below or send "
            f"<code>{command} {next_start}-{page_end}</code> for the next batch."
        )
    log.info("%s batch: done %d stocks in %.1fs", command, len(work), monotonic() - started_at)
    return all_lines, next_start


def handle_stock_batch(chat_id, command: str, range, deep: bool) -> None:
    """Render /fundamentalanalyze or /fundamentalreport for a range of watchlist positions."""
    lines, next_start = build_stock_batch(chat_id, command, range, deep)
    markup = stock_next_markup(deep, next_start) if next_start else None
    reply_messages(chat_id, split_messages(lines), reply_markup=markup)


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

    range = parse_stock_range(parts[1])
    if range is not None:
        handle_stock_batch(chat_id, "/fundamentalreport", range, deep=True)
        return

    raw_symbol = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    started_at = monotonic()
    log.info("handle_fund: deep fundamentals for %s (chat %s)", raw_symbol, chat_id)

    quote = get_quote("NSE", raw_symbol) or get_quote("BSE", raw_symbol) or {}
    is_us = False
    if quote.get("price") is None:
        us_quote = get_quote("US", raw_symbol) or {}
        if us_quote.get("price") is not None or us_quote.get("name"):
            quote = us_quote
            is_us = True
    fund = (get_us_fundamentals(raw_symbol) if is_us
            else get_fundamentals(raw_symbol, with_screener=True)) or {}

    if quote.get("price") is None and not fund:
        reply_suggestions(chat_id, raw_symbol, "fundamentalreport")
        return

    if is_us:
        lines = _us_stock_lines(raw_symbol, quote, fund, include_tip=True)
    else:
        lines = _fund_report_lines(raw_symbol, quote, fund, include_tip=True)
    reply_messages(chat_id, split_messages(lines))
    log.info("handle_fund: completed for %s in %.1fs", raw_symbol, monotonic() - started_at)
