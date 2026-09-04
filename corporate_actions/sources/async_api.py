"""Async wrappers for existing blocking source functions.

These are thin, best-effort wrappers that run the existing sync functions in
`asyncio.to_thread` so callers can `await` them without blocking the event
loop. Over time the underlying implementations can be converted to native
`httpx` async calls and the wrappers removed.
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import (
    get_quote as _get_quote,
    get_fundamentals as _get_fundamentals,
)

try:
    # import optional functions (may not exist in older code)
    from .nse import get_nse_stock_list as _get_nse_stock_list, get_nse_corporate_actions as _get_nse_corporate_actions, search_stocks as _search_stocks
    from .bse import get_bse_stock_list as _get_bse_stock_list, get_bse_corporate_actions as _get_bse_corporate_actions
    from .screener import parse_screener_fundamentals as _parse_screener_fundamentals, get_sector_pe as _get_sector_pe
    from .universe import get_index_universe as _get_index_universe
    from .ohlc import get_ohlc as _get_ohlc
    from .news import get_stock_news as _get_stock_news
    from .us_fundamentals import get_us_fundamentals as _get_us_fundamentals
except Exception:
    # If any import fails, wrap functions will still attempt to call via
    # attribute access on import-time resolution by callers.
    _get_nse_stock_list = None
    _get_nse_corporate_actions = None
    _search_stocks = None
    _get_bse_stock_list = None
    _get_bse_corporate_actions = None
    _parse_screener_fundamentals = None
    _get_sector_pe = None
    _get_index_universe = None
    _get_ohlc = None
    _get_stock_news = None
    _get_us_fundamentals = None


async def get_quote_async(exchange: str, symbol: str) -> dict | None:
    try:
        # prefer native async if provided by module
        from .quotes import get_quote_async as native

        return await native(exchange, symbol)
    except Exception:
        return await asyncio.to_thread(_get_quote, exchange, symbol)


async def get_fundamentals_async(symbol: str, with_screener: bool = True) -> dict | None:
    try:
        from .fundamentals import get_fundamentals_async as native
        return await native(symbol, with_screener)
    except Exception:
        return await asyncio.to_thread(_get_fundamentals, symbol, with_screener)


def _wrap_sync(fn):
    async def _inner(*args: Any, **kwargs: Any):
        return await asyncio.to_thread(fn, *args, **kwargs)

    return _inner


# Best-effort wrappers for other potentially blocking functions
get_nse_stock_list_async = _wrap_sync(_get_nse_stock_list) if _get_nse_stock_list else None
get_nse_corporate_actions_async = _wrap_sync(_get_nse_corporate_actions) if _get_nse_corporate_actions else None
search_stocks_async = _wrap_sync(_search_stocks) if _search_stocks else None
get_bse_stock_list_async = _wrap_sync(_get_bse_stock_list) if _get_bse_stock_list else None
get_bse_corporate_actions_async = _wrap_sync(_get_bse_corporate_actions) if _get_bse_corporate_actions else None
parse_screener_fundamentals_async = _wrap_sync(_parse_screener_fundamentals) if _parse_screener_fundamentals else None
get_sector_pe_async = _wrap_sync(_get_sector_pe) if _get_sector_pe else None
get_index_universe_async = _wrap_sync(_get_index_universe) if _get_index_universe else None
get_ohlc_async = _wrap_sync(_get_ohlc) if _get_ohlc else None
get_stock_news_async = _wrap_sync(_get_stock_news) if _get_stock_news else None
get_us_fundamentals_async = _wrap_sync(_get_us_fundamentals) if _get_us_fundamentals else None
