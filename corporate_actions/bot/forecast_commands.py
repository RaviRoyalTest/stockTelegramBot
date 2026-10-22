"""Analyst forecast command: /forecast SYMBOL (aliases /analyst, /forecastanalysis).

Shows the "forecast value" for a stock: analyst consensus + rating breakdown,
the 12-month target price with upside, the top executives, and (for NSE
stocks) the top competitors by market cap. Works for Indian (NSE/BSE) and US
(NASDAQ/NYSE) stocks - the market is auto-detected. Unknown symbols get the
usual ticker + full-name suggestion pick-list.
"""
from __future__ import annotations

import logging

from ..core.text import split_messages
from ..formatting.forecast import build_forecast_lines
from ..sources import (
    get_best_quote,
    get_fundamentals,
    get_quote,
    get_us_fundamentals,
    normalise_fundamentals,
)
from .helpers import has_stock_data, reply_suggestions
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


def handle_forecast(chat_id, parts) -> None:
    """Analyst forecast + executives + competitors for one symbol."""
    if len(parts) < 2:
        reply(chat_id, _USAGE)
        return

    raw_symbol = parts[1].upper().strip()

    # Auto-detect the market: NSE \u2192 BSE \u2192 US (same order as /indicator).
    try:
        quote = get_best_quote("NSE", raw_symbol) or {}
    except Exception:
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
        try:
            fund = normalise_fundamentals(raw_symbol, dict(fund), quote or {})
        except Exception as error:
            log.warning("handle_forecast: %s", error)
            pass

    if not has_stock_data(raw_symbol, quote, fund):
        log.info("forecast: no data for %s - showing suggestions", raw_symbol)
        reply_suggestions(chat_id, raw_symbol, "forecast")
        return

    log.info("forecast: %s (%s) -> %d fund fields", raw_symbol, "US" if is_us else "IN", len(fund))
    lines = build_forecast_lines(raw_symbol, quote, fund, us=is_us)
    reply_messages(chat_id, split_messages(lines))
