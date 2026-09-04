"""Provider-based market search fallback used when direct exchange endpoints fail.

This keeps the app usable even when the native source is unreachable. The
module intentionally prefers a clean, minimal schema so the dashboard and bot
can reuse it without changing call sites:

    [{"symbol": "RELIANCE", "name": "Reliance Industries", "exchange": "NSE"}]

It combines safe HTTP lookups against public sources with a deliberately
lightweight response parser.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from .. import config
from .http import _quote_session
from .nse import search_stocks
from .us_search import search_us_tickers

log = logging.getLogger(__name__)

_PROVIDER_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)


def _dedupe_and_rank(matches: list[dict], query: str) -> list[dict]:
    """Keep the best result per symbol and rank by relevance."""
    seen: set[str] = set()
    out: list[dict] = []
    needle = (query or "").upper().strip()
    for item in matches:
        symbol = (item.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(item)

    def _score(item: dict) -> tuple[int, int, str]:
        symbol = (item.get("symbol") or "").upper()
        name = (item.get("name") or "").upper()
        if symbol == needle:
            pri = 0
        elif symbol.startswith(needle):
            pri = 1
        elif name.startswith(needle):
            pri = 2
        elif needle in symbol:
            pri = 3
        elif needle in name:
            pri = 4
        else:
            pri = 5
        return pri, len((item.get("name") or "")), symbol

    out.sort(key=_score)
    return out


def search_market_data(query: str, filters: dict | None = None, limit: int = 8) -> list[dict]:
    """Return the best symbol suggestions from available public providers.

    The resolver tries the native market source first (NSE search for India,
    Yahoo US search for US) and falls back to Yahoo search when the direct path
    is empty or blocked. Results are deduplicated and ranked, so the UI always
    gets the most relevant matches without a dead-end search.
    """
    term = (query or "").strip()
    if not term:
        return []
    filters = filters or {}
    market = str(filters.get("market", "in")).lower()
    max_results = max(1, int(filters.get("limit") or limit))

    candidates: list[dict] = []
    if market in {"in", "india", "nse", "bse"}:
        candidates.extend(search_stocks(term, limit=max_results))
    else:
        candidates.extend(search_us_tickers(term, limit=max_results))

    for host in _PROVIDER_HOSTS:
        url = f"{host}/v1/finance/search?q={quote(term)}&quotesCount={max_results}&newsCount=0"
        try:
            response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            response.raise_for_status()
            payload = response.json() or {}
            for item in payload.get("quotes") or []:
                if not isinstance(item, dict):
                    continue
                quote_type = (item.get("quoteType") or "").upper()
                if quote_type and quote_type != "EQUITY":
                    continue
                symbol = (item.get("symbol") or "").strip()
                if not symbol:
                    continue
                name = (item.get("longname") or item.get("shortname") or "").strip()
                exchange = (item.get("exchDisp") or "").strip() or (item.get("exchange") or "").strip()
                if market in {"us", "usa", "nasdaq", "nyse"}:
                    exchange_code = (item.get("exchange") or "").upper()
                    allowed = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "BAT", "OQB", "OQX", "OQO", "PNK"}
                    if exchange_code and exchange_code not in allowed:
                        continue
                elif not exchange:
                    exchange = "NSE"
                candidates.append({"symbol": symbol, "name": name, "exchange": exchange})
            if candidates:
                break
        except Exception as error:  # pragma: no cover - runtime resilience path
            log.info("provider fallback search failed on %s for %r: %s", host, term, error)
            continue

    ordered = _dedupe_and_rank(candidates, term)
    return ordered[:max_results]


def get_company_profile(symbol: str, exchange: str = "NSE") -> dict:
    """Lightweight company profile lookup from the best available provider."""
    term = (symbol or "").strip()
    if not term:
        return {}
    try:
        market = "us" if str(exchange or "").upper() == "US" else "in"
        results = search_market_data(term, filters={"market": market, "limit": 5})
        for row in results:
            if row.get("symbol", "").upper() == term.upper():
                return row
    except Exception as error:  # pragma: no cover - runtime resilience path
        log.info("company profile lookup failed for %s: %s", term, error)
    return {}
