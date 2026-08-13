"""Data-acquisition package.

One module per concern (NSE, BSE, quotes, news, universes, OHLC, screener.in,
Yahoo fundamentals for India + us_fundamentals for US tickers) - see
AGENTS.md for the package layout. This facade re-exports the public API so
callers can `from corporate_actions.sources import get_quote` (or
`from corporate_actions import sources`) exactly as before.
"""
from .bse import get_bse_corporate_actions, get_bse_stock_list
from .errors import SourceError
from .fundamentals import FUND_MAX_ROWS, get_fundamentals
from .us_fundamentals import get_us_fundamentals
from .us_search import search_us_tickers
from .news import get_stock_news
from .nse import (
    get_nse_corporate_actions,
    get_nse_stock_list,
    get_nse_stock_list_cached,
    search_stocks,
)
from .ohlc import OHLC_TIMEFRAMES, _HIGHER_TIMEFRAME_LADDER, get_index_ohlc, get_ohlc
from .quotes import get_quote
from .rights import RIGHTS_OFFER_WINDOWS, attach_rights_windows
from .screener import get_sector_pe, parse_screener_fundamentals
from .types import ACTION_TYPES, INCREASE_TYPES, TYPE_LABELS, action_type, pick
from .universe import (
    get_daily_change,
    get_gap_change,
    get_gap_history,
    get_index_universe,
    get_intraday_change,
    get_window_gap_change,
    universe_exchange,
)
from .us_corporate_actions import get_us_corporate_actions

__all__ = [
    "SourceError",
    "ACTION_TYPES",
    "TYPE_LABELS",
    "INCREASE_TYPES",
    "action_type",
    "pick",
    "get_quote",
    "get_nse_stock_list",
    "get_nse_stock_list_cached",
    "search_stocks",
    "get_nse_corporate_actions",
    "get_bse_stock_list",
    "get_bse_corporate_actions",
    "get_us_corporate_actions",
    "get_stock_news",
    "get_index_universe",
    "get_intraday_change",
    "get_daily_change",
    "get_gap_change",
    "get_gap_history",
    "get_window_gap_change",
    "universe_exchange",
    "get_ohlc",
    "get_index_ohlc",
    "OHLC_TIMEFRAMES",
    "_HIGHER_TIMEFRAME_LADDER",
    "get_sector_pe",
    "parse_screener_fundamentals",
    "get_fundamentals",
    "get_us_fundamentals",
    "search_us_tickers",
    "FUND_MAX_ROWS",
    "RIGHTS_OFFER_WINDOWS",
    "attach_rights_windows",
]
