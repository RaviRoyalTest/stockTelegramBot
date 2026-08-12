"""US-stock details command: /usstock TICKER (aliases /usfund, /usquote, /us).

Fetches live quote + deep fundamentals for a US ticker (AAPL, MSFT, ...)
straight from Yahoo Finance without the .NS exchange suffix the Indian
commands use. Prices/money render in USD (market cap in $B); there is no
screener.in part (India-only). Works with the scheduling system exactly like
any other command: /schedule add 3h /usstock AAPL us runs it only during US
market hours.

When Yahoo does not know the ticker, a search for the same name is run and
the closest matches (symbol + full company name + exchange) are suggested so
the user can tap the right one instead of guessing.
"""
from __future__ import annotations

import logging

from ..core.text import escape, split_messages
from ..formatting.stock_us import _us_stock_lines
from ..sources import get_quote, get_us_fundamentals, search_us_tickers
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_US_USAGE = (
    "Usage: <code>/usstock TICKER</code> (e.g. <code>/usstock AAPL</code>, "
    "<code>/usstock MSFT</code>, <code>/usstock NVDA</code>)\n"
    "Aliases: <code>/usfund</code>, <code>/usquote</code>, <code>/us</code>\n"
    "Schedule it: <code>/schedule add 3h /usstock AAPL us</code> "
    "(runs only during US market hours)."
)


def _not_found_message(raw_symbol: str, matches: list[dict]) -> str:
    """Clear 'not found' reply - with ticker + full-name suggestions when any."""
    exact = [m for m in matches if m["symbol"].upper() == raw_symbol]
    if exact:
        # Yahoo knows the ticker but live data failed - likely a transient issue.
        return (
            f"⚠️ <code>{escape(raw_symbol)}</code> exists on Yahoo but live data "
            "isn't available right now.\nTry again in a minute."
        )
    if not matches:
        return (
            f"🚫 No US data found for <code>{escape(raw_symbol)}</code> — Yahoo "
            "has no match for that name.\nCheck the spelling (e.g. "
            "<code>/usstock AAPL</code>, <code>/usstock BRK-B</code>, "
            "<code>/usstock BF.B</code>)."
        )
    lines = [
        f"🚫 No US data found for <code>{escape(raw_symbol)}</code>.",
        "",
        "Did you mean one of these US tickers?",
    ]
    for match in matches[:6]:
        name = escape(match.get("name") or "")
        exchange = match.get("exchange") or ""
        exchange_tag = f" ({escape(exchange)})" if exchange else ""
        lines.append(f"• <code>{escape(match['symbol'])}</code> — {name}{exchange_tag}")
    lines.append("")
    lines.append(f"Try: <code>/usstock {escape(matches[0]['symbol'])}</code>")
    return "\n".join(lines)


def handle_us_stock(chat_id, parts) -> None:
    """Deep fundamentals report for one US ticker (/usstock AAPL)."""
    if len(parts) < 2:
        reply(chat_id, _US_USAGE)
        return

    raw_symbol = parts[1].upper().strip().removesuffix(".US")
    if not raw_symbol or len(raw_symbol) > 10 or not raw_symbol.replace(".", "").isalnum():
        reply(chat_id, f"Bad ticker <code>{escape(parts[1])}</code>. Use e.g. <code>/usstock AAPL</code>.")
        return

    log.info("handle_us_stock: fetching US fundamentals for %s (chat %s)", raw_symbol, chat_id)
    quote = get_quote("US", raw_symbol) or {}
    fund = get_us_fundamentals(raw_symbol) or {}

    if quote.get("price") is None and not fund:
        matches = search_us_tickers(raw_symbol, limit=6)
        reply(chat_id, _not_found_message(raw_symbol, matches))
        return

    lines = _us_stock_lines(raw_symbol, quote, fund, include_tip=True)
    reply_messages(chat_id, split_messages(lines))
    log.info("handle_us_stock: completed for %s", raw_symbol)
