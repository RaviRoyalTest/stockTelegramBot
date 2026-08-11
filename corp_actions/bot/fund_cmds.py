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
from ..formatting.stock import _fund_report_lines, _stock_summary_lines
from ..sources import get_fundamentals, get_quote
from .helpers import MAX_FUND_BATCH, MAX_STOCK_BATCH, reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def parse_stock_range(arg: str):
    """Parse a watchlist position range like '5', '5-10', '5 - 10', 'all'.

    Returns a (start, end) tuple with 1-based inclusive positions (end None =
    to the end of the list), or None when the arg looks like a stock symbol.
    """
    token = (arg or "").strip().lower()
    if not token:
        return None
    if token in ("all", "mylist", "my-list", "my list", "*", "full", "everything"):
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

    rng = parse_stock_range(parts[1])
    if rng is not None:
        handle_stock_batch(chat_id, "/fundamentalanalyze", rng, deep=False)
        return

    raw_sym = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    t0 = monotonic()
    log.info("handle_single_stock: fetching full details for %s for chat %s", raw_sym, chat_id)

    quote = get_quote("NSE", raw_sym) or get_quote("BSE", raw_sym) or {}
    fund = get_fundamentals(raw_sym, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        reply_suggestions(chat_id, raw_sym, "fundamentalanalyze")
        return

    lines = _stock_summary_lines(raw_sym, quote, fund, include_tip=True)
    reply_messages(chat_id, ["\n".join(lines)])
    log.info("handle_single_stock: completed for %s in %.1fs", raw_sym, monotonic() - t0)


def stock_next_markup(deep: bool, start: int) -> dict:
    """Inline 'Next' button for a paginated batch (callback_data stknext:deep:start)."""
    return {
        "inline_keyboard": [
            [{"text": "Next \u25b6", "callback_data": f"stknext:{1 if deep else 0}:{start}"}],
        ]
    }


def build_stock_batch(chat_id, cmd: str, rng, deep: bool) -> tuple[list[str], int | None]:
    """Fetch and format a page of watchlist positions (never sends).

    Returns (lines, next_start) where next_start is the 1-based start of the
    next page, or None when this is the last page.
    """
    cap = MAX_FUND_BATCH if deep else MAX_STOCK_BATCH
    items = storage.get_user_list(chat_id)
    if not items:
        return ["Your watchlist is empty. Add stocks with /addstock SYMBOL NSE"], None
    start, end = rng
    total = len(items)
    start = max(1, start)
    end = total if end is None else min(end, total)
    if start > total:
        return [
            f"Your watchlist has only {total} stock(s) - start position must be 1..{total}."
        ], None
    work = items[start - 1:end][:cap]
    t0 = monotonic()
    log.info(
        "%s batch: positions %d-%d (%d stocks, deep=%s)",
        cmd, start, start + len(work) - 1, len(work), deep,
    )

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(work)))) as ex:
        quotes = list(ex.map(lambda it: get_quote(it["exchange"], it["symbol"]) or {}, work))
        funds = list(ex.map(lambda it: get_fundamentals(it["symbol"], with_screener=True) or {}, work))

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
                f"\U0001F4CA {label} <b>{escape(sym)}</b>",
                "\u26a0\ufe0f No data available right now.",
            ]
        body.extend(lines)
        body.append("")

    header = (
        f"\U0001F4CA <b>{cmd.upper()} \u00b7 Watchlist positions "
        f"{start}\u2013{start + len(work) - 1} of {total}</b>\n"
    )
    all_lines = [header] + body
    next_start = start + len(work) if start + len(work) <= total else None
    if next_start is not None:
        remaining = total - (start + len(work) - 1)
        page_end = min(total, next_start + cap - 1)
        all_lines.append(
            f"\u2026 and {remaining} more. Tap <b>Next \u25b6</b> below or send "
            f"<code>{cmd} {next_start}-{page_end}</code> for the next batch."
        )
    log.info("%s batch: done %d stocks in %.1fs", cmd, len(work), monotonic() - t0)
    return all_lines, next_start


def handle_stock_batch(chat_id, cmd: str, rng, deep: bool) -> None:
    """Render /fundamentalanalyze or /fundamentalreport for a range of watchlist positions."""
    lines, next_start = build_stock_batch(chat_id, cmd, rng, deep)
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

    rng = parse_stock_range(parts[1])
    if rng is not None:
        handle_stock_batch(chat_id, "/fundamentalreport", rng, deep=True)
        return

    raw_sym = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    t0 = monotonic()
    log.info("handle_fund: deep fundamentals for %s (chat %s)", raw_sym, chat_id)

    quote = get_quote("NSE", raw_sym) or get_quote("BSE", raw_sym) or {}
    fund = get_fundamentals(raw_sym, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        reply_suggestions(chat_id, raw_sym, "fundamentalreport")
        return

    lines = _fund_report_lines(raw_sym, quote, fund, include_tip=True)
    reply_messages(chat_id, split_messages(lines))
    log.info("handle_fund: completed for %s in %.1fs", raw_sym, monotonic() - t0)
