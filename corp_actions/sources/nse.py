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
        resp = _session().get(config.NSE_STOCK_LIST_URL, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
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
    except requests.RequestException as exc:
        raise SourceError(f"NSE stock list request failed: {exc}") from exc
    except Exception as exc:  # csv.DictReader can raise Error
        raise SourceError(f"NSE stock list parse failed: {exc}") from exc


_nse_list_cache = None
_nse_list_cache_ts = 0.0
_NSE_LIST_TTL = 3600  # seconds


def get_nse_stock_list_cached() -> list[dict]:
    """NSE stock list with a 1h in-process cache (used by the bot's search)."""
    global _nse_list_cache, _nse_list_cache_ts
    now = time.time()
    if _nse_list_cache and now - _nse_list_cache_ts < _NSE_LIST_TTL:
        return _nse_list_cache
    _nse_list_cache = get_nse_stock_list()
    _nse_list_cache_ts = now
    return _nse_list_cache


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """Case-insensitive substring search of the NSE list by symbol or company name."""
    q = (query or "").upper()
    try:
        stocks = get_nse_stock_list_cached()
    except SourceError as exc:
        log.warning("NSE stock list unavailable for search: %s", exc)
        return []
    matches = [
        s for s in stocks
        if q in s["symbol"].upper() or q in s["company"].upper()
    ]
    return matches[:limit]


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
        resp = _session().get(url, params=params, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SourceError(f"NSE corporate actions request failed: {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"NSE corporate actions bad JSON: {exc}") from exc

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
                "bc_start": parse_nse_date(pick(item, "bcStartDate", default="-")),
                "bc_end": parse_nse_date(pick(item, "bcEndDate", default="-")),
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
