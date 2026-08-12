"""NSE stock list, corporate actions and symbol search."""
from __future__ import annotations

import csv
import io
import logging
import time

import requests

from .. import config
from ..core.dates import parse_nse_date
from .errors import SourceError
from .http import _session
from .rights import attach_rights_windows
from .types import pick

log = logging.getLogger(__name__)


def get_nse_stock_list() -> list[dict]:
    """Return list of {'symbol', 'company', 'exchange'} for NSE equities."""
    try:
        response = _session().get(config.NSE_STOCK_LIST_URL, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        text = response.text
        # Strip the UTF-8 BOM if present.
        if text.startswith("\ufeff"):
            text = text[1:]
        reader = csv.DictReader(io.StringIO(text))
        stocks = []
        for row in reader:
            symbol = (row.get("SYMBOL") or "").strip()
            company = (row.get("NAME OF COMPANY") or "").strip()
            if symbol and symbol.lower() not in ("symbol", "index"):
                stocks.append(
                    {"symbol": symbol, "company": company, "exchange": "NSE"}
                )
        if not stocks:
            raise SourceError("NSE stock list parsed but empty")
        log.info("NSE stock list loaded: %d equities", len(stocks))
        return stocks
    except SourceError:
        raise
    except requests.RequestException as error:
        raise SourceError(f"NSE stock list request failed: {error}") from error
    except Exception as error:  # csv.DictReader can raise Error
        raise SourceError(f"NSE stock list parse failed: {error}") from error


_nse_list_cache = None
_nse_list_cache_ts = 0.0
_NSE_LIST_CACHE_SECONDS = 3600  # seconds


def get_nse_stock_list_cached() -> list[dict]:
    """NSE stock list with a 1h in-process cache (used by the bot's search)."""
    global _nse_list_cache, _nse_list_cache_ts
    now = time.time()
    if _nse_list_cache and now - _nse_list_cache_ts < _NSE_LIST_CACHE_SECONDS:
        return _nse_list_cache
    _nse_list_cache = get_nse_stock_list()
    _nse_list_cache_ts = now
    return _nse_list_cache


def _fuzzy_stock_matches(stocks: list[dict], query: str, limit: int) -> list[dict]:
    """Typo-tolerant close matches on symbol, then on company name (difflib).

    Only reached when the substring search found nothing, so a misspelling
    like 'tatamotrs' still yields TATAMOTORS instead of a dead-end reply.
    """
    from difflib import get_close_matches

    by_symbol, by_name, symbols, names = {}, {}, [], []
    for stock in stocks:
        symbol = stock["symbol"].upper()
        company = stock["company"].upper()
        if symbol not in by_symbol:
            by_symbol[symbol] = stock
            symbols.append(symbol)
        if company and company not in by_name:
            by_name[company] = stock
            names.append(company)
    seen, out = set(), []
    for candidate in get_close_matches(query, symbols, n=limit, cutoff=0.55):
        stock = by_symbol[candidate]
        if id(stock) not in seen:
            seen.add(id(stock))
            out.append(stock)
    if len(out) < limit:
        for candidate in get_close_matches(query, names, n=limit, cutoff=0.55):
            stock = by_name[candidate]
            if id(stock) not in seen:
                seen.add(id(stock))
                out.append(stock)
    return out[:limit]


def _match_rank(stock: dict, query: str) -> int:
    """Relevance rank for substring matches (lower = more relevant).

    Exact symbol, then symbol-prefix, then company-prefix, then plain
    contains - so 'reliance' puts RELIANCE ahead of RCOM even though the NSE
    list is alphabetical.
    """
    symbol = stock["symbol"].upper()
    company = stock["company"].upper()
    if symbol == query:
        return 0
    if symbol.startswith(query):
        return 1
    if company.startswith(query):
        return 2
    if query in symbol:
        return 3
    return 4  # query in company


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """Search the NSE list by symbol or company name (substring, then fuzzy).

    First a case-insensitive substring match on symbol OR company name,
    ranked most-relevant first ('reliance ind' -> RELIANCE); when that finds
    nothing, a typo-tolerant fuzzy pass on symbol then company name
    ('HDFCBNAK' -> HDFCBANK).
    """
    query = (query or "").upper().strip()
    try:
        stocks = get_nse_stock_list_cached()
    except SourceError as error:
        log.warning("NSE stock list unavailable for search: %s", error)
        return []
    matches = [
        stock for stock in stocks
        if query in stock["symbol"].upper() or query in stock["company"].upper()
    ]
    if matches:
        # rank by relevance, then shorter company name = the primary listing
        # ('Reliance Industries' before 'Reliance Industrial Infrastructure').
        matches.sort(key=lambda stock: (_match_rank(stock, query), len(stock["company"])))
        return matches[:limit]
    return _fuzzy_stock_matches(stocks, query, limit)


def get_nse_corporate_actions(symbol: str | None = None) -> list[dict]:
    """Return corporate actions announced on NSE, normalised records.

    When `symbol` is given, the NSE API returns the full history for that
    specific symbol (the unfiltered feed only returns ~20 most-recent
    records, which usually misses most watchlist stocks).
    """
    url = config.NSE_ACTIONS_URL
    params = {}
    if symbol:
        params["symbol"] = symbol
    try:
        response = _session().get(url, params=params, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        raise SourceError(f"NSE corporate actions request failed: {error}") from error
    except ValueError as error:
        raise SourceError(f"NSE corporate actions bad JSON: {error}") from error

    if not isinstance(data, list):
        raise SourceError("NSE corporate actions returned unexpected payload")

    records = []
    for item in data:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "symbol": pick(item, "symbol"),
                "company": pick(item, "comp"),
                "exchange": "NSE",
                "subject": pick(item, "subject"),
                "ex_date": parse_nse_date(pick(item, "exDate", default="-")),
                "record_date": parse_nse_date(pick(item, "recDate", default="-")),
                "announcement_date": parse_nse_date(
                    pick(item, "caBroadcastDate", default="-")
                ),
                "book_closure_start": parse_nse_date(pick(item, "bcStartDate", default="-")),
                "book_closure_end": parse_nse_date(pick(item, "bcEndDate", default="-")),
                "rights_start": parse_nse_date(
                    pick(item, "rightsStartDate", "rightsSubStartDate", "offerStartDate", default="-")
                ),
                "rights_end": parse_nse_date(
                    pick(item, "rightsEndDate", "rightsSubEndDate", "offerEndDate", default="-")
                ),
                "face_value": pick(item, "faceVal"),
                "isin": pick(item, "isin"),
                "series": pick(item, "series"),
            }
        )
    attach_rights_windows(records)
    log.info("NSE corporate actions fetched: %d record(s) (symbol=%s)", len(records), symbol or "all")
    return records
