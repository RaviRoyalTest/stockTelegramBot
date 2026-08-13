"""Analyst forecast command: /forecast SYMBOL (aliases /analyst, /forecastanalysis).

Shows the "forecast value" for a stock: analyst consensus + rating breakdown,
the 12-month target price with upside, the top executives, and (for NSE
stocks) the top competitors by market cap. Works for Indian (NSE/BSE) and US
(NASDAQ/NYSE) stocks - the market is auto-detected. Unknown symbols get the
usual ticker + full-name suggestion pick-list.
"""
from __future__ import annotations

import logging

from ..core.text import escape, split_messages
from ..formatting.forecast import build_forecast_lines
from ..sources import get_fundamentals, get_quote, get_us_fundamentals, search_stocks, search_us_tickers
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_USAGE = (
    "Usage: <code>/forecast SYMBOL</code> (e.g. <code>/forecast RELIANCE</code>, "
    "<code>/forecast AAPL</code>)\n"
    "Shows the forecast value for a stock: analyst consensus & rating breakdown,\n"
    "the 12-month target price with upside, the top executives, and (NSE stocks)\n"
    "the top competitors by market cap.\n"
    "Aliases: <code>/analyst</code>, <code>/forecastanalysis</code>"
)


def _not_found_message(raw_symbol: str, in_matches: list[dict], us_matches: list[dict]) -> str:
    """Clear 'not found' reply with ticker + full-name suggestions (IN then US)."""
    matches = (in_matches[:3] + us_matches[:3]) or None
    if not matches:
        return (
            f"\U0001F6AB No market data found for <code>{escape(raw_symbol)}</code> "
            "\u2014 neither NSE/BSE nor NASDAQ/NYSE knows that name.\n"
            "Check the spelling (e.g. <code>/forecast RELIANCE</code> or "
            "<code>/forecast AAPL</code>)."
        )
    lines = [
        f"\U0001F6AB No market data found for <code>{escape(raw_symbol)}</code>.",
        "",
        "Did you mean one of these?",
    ]
    for match in matches:
        name = escape(match.get("name") or match.get("company") or "")
        exchange = (match.get("exchange") or "").upper()
        lines.append(f"\u2022 <code>{escape(match['symbol'])}</code> \u2014 {name} ({exchange})")
    lines.append("")
    lines.append(f"Try: <code>/forecast {escape(matches[0]['symbol'])}</code>")
    return "\n".join(lines)


def handle_forecast(chat_id, parts) -> None:
    """Analyst forecast + executives + competitors for one symbol."""
    if len(parts) < 2:
        reply(chat_id, _USAGE)
        return

    raw_symbol = parts[1].upper().strip()

    # Auto-detect the market: NSE \u2192 BSE \u2192 US (same order as /indicator).
    quote = get_quote("NSE", raw_symbol) or get_quote("BSE", raw_symbol) or {}
    is_us = False
    if quote.get("price") is None:
        us_quote = get_quote("US", raw_symbol) or {}
        if us_quote.get("price") is not None or us_quote.get("name"):
            quote = us_quote
            is_us = True
    if is_us:
        fund = get_us_fundamentals(raw_symbol) or {}
    else:
        fund = get_fundamentals(raw_symbol, with_screener=True) or {}

    if quote.get("price") is None and not fund:
        log.info("forecast: no data for %s - showing suggestions", raw_symbol)
        reply(chat_id, _not_found_message(
            raw_symbol,
            search_stocks(raw_symbol, limit=5),
            search_us_tickers(raw_symbol, limit=5),
        ))
        return

    log.info("forecast: %s (%s) -> %d fund fields", raw_symbol, "US" if is_us else "IN", len(fund))
    lines = build_forecast_lines(raw_symbol, quote, fund, us=is_us)
    reply_messages(chat_id, split_messages(lines))
