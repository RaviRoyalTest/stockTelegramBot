"""US ticker search (Yahoo Finance /v1/finance/search) for name suggestions.

A tiny IO facade: one HTTP call to Yahoo's search endpoint with pure parsing
helpers for the JSON payload. Used by /usstock when an exact ticker does not
resolve, so the user gets symbol + full-name suggestions instead of a
dead-end error. Never raises - returns [] on any failure.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from .. import config
from .http import _quote_session

log = logging.getLogger(__name__)

_SEARCH_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)
# exchDisp suffixes to strip: 'NasdaqGS' -> 'NASDAQ', 'NYSEArca' -> 'NYSE'
_EXCHANGE_SUFFIXES = ("GS", "CM", "GM", "ARCA", "MKT", "AMEX")
# Yahoo market codes for US listings only - keeps foreign listings (Frankfurt,
# XETRA, SET, WSE, NEO, ...) out of the suggestions.
_US_EXCHANGE_CODES = {
    "NMS", "NGM", "NCM",  # NASDAQ
    "NYQ",                  # NYSE
    "ASE",                  # NYSE American (AMEX)
    "PCX",                  # NYSE Arca
    "BTS", "BAT",          # Cboe BZX / Bats
    "OQB", "OQX", "OQO", "PNK",  # OTCQB / OTCQX / OTC Pink
}


def _exchange_label(exch_disp: str) -> str:
    """Normalise a Yahoo exchange display name ('NasdaqGS' -> 'NASDAQ')."""
    if not exch_disp:
        return ""
    label = exch_disp.upper()
    for suffix in _EXCHANGE_SUFFIXES:
        if label.endswith(suffix) and len(label) > len(suffix):
            label = label[: -len(suffix)]
            break
    return label


def _parse_quotes(payload: dict, limit: int = 6) -> list[dict]:
    """Equity suggestions from Yahoo search JSON.

    Returns [{'symbol', 'name', 'exchange'}] keeping only EQUITY quotes
    (Yahoo mixes in crypto/ETF/futures/indices - skip those).
    """
    quotes = payload.get("quotes") or []
    out = []
    for item in quotes:
        if not isinstance(item, dict):
            continue
        quote_type = (item.get("quoteType") or "").upper()
        if quote_type and quote_type != "EQUITY":
            continue
        if (item.get("exchange") or "").upper() not in _US_EXCHANGE_CODES:
            continue
        symbol = (item.get("symbol") or "").strip()
        if not symbol:
            continue
        name = (item.get("longname") or item.get("shortname") or "").strip()
        out.append(
            {
                "symbol": symbol,
                "name": str(name),
                "exchange": _exchange_label(item.get("exchDisp") or ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def search_us_tickers(query: str, limit: int = 6) -> list[dict]:
    """Best-effort Yahoo ticker search: symbol + full name + exchange.

    Returns [] when the query is empty, Yahoo has no equity matches, or the
    endpoint fails (tries query1 then query2). Never raises.
    """
    query = (query or "").strip()
    if not query:
        return []
    for host in _SEARCH_HOSTS:
        url = (
            f"{host}/v1/finance/search"
            f"?q={quote(query)}&quotesCount=10&newsCount=0"
        )
        try:
            response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            response.raise_for_status()
            matches = _parse_quotes(response.json(), limit=limit)
            if matches:
                return matches
        except Exception as error:
            log.info("US ticker search failed on %s for %r - %s", host, query, error)
            continue
    return []
