"""US-stock details command: /usstock TICKER (aliases /usfund, /usquote, /us).

Fetches live quote + deep fundamentals for a US ticker (AAPL, MSFT, ...)
straight from Yahoo Finance without the .NS exchange suffix the Indian
commands use. Prices/money render in USD (market cap in $B); there is no
screener.in part (India-only). Works with the scheduling system exactly like
any other command: /schedule add 3h /usstock AAPL us runs it only during US
market hours.
"""
from __future__ import annotations

import logging

from ..core.text import escape, split_messages
from ..formatting.stock import _us_stock_lines
from ..sources import get_quote, get_us_fundamentals
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def handle_us_stock(chat_id, parts) -> None:
    """Deep fundamentals report for one US ticker (/usstock AAPL)."""
    if len(parts) < 2:
        reply(
            chat_id,
            "Usage: <code>/usstock TICKER</code> (e.g. <code>/usstock AAPL</code>, "
            "<code>/usstock MSFT</code>, <code>/usstock NVDA</code>)\\n"
            "Aliases: <code>/usfund</code>, <code>/usquote</code>, <code>/us</code>\\n"
            "Schedule it: <code>/schedule add 3h /usstock AAPL us</code> "
            "(runs only during US market hours).",
        )
        return

    raw_symbol = parts[1].upper().strip().removesuffix(".US")
    if not raw_symbol or len(raw_symbol) > 10 or not raw_symbol.replace(".", "").isalnum():
        reply(chat_id, f"Bad ticker <code>{escape(parts[1])}</code>. Use e.g. <code>/usstock AAPL</code>.")
        return

    log.info("handle_us_stock: fetching US fundamentals for %s (chat %s)", raw_symbol, chat_id)
    quote = get_quote("US", raw_symbol) or {}
    fund = get_us_fundamentals(raw_symbol) or {}

    if quote.get("price") is None and not fund:
        reply(
            chat_id,
            f"\\U0001F6AB No US data found for <code>{escape(raw_symbol)}</code>.\\n"
            "Check the ticker (e.g. <code>/usstock AAPL</code>, <code>/usstock BRK-B</code>, "
            "<code>/usstock BF.B</code>) - Yahoo may not know this symbol.",
        )
        return

    lines = _us_stock_lines(raw_symbol, quote, fund, include_tip=True)
    reply_messages(chat_id, split_messages(lines))
    log.info("handle_us_stock: completed for %s", raw_symbol)
