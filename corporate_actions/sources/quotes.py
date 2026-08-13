"""Live quotes from Yahoo Finance (public, no key)."""
from __future__ import annotations

import logging
import time

from .. import config
from .http import _quote_session, _throttle_chart_req

log = logging.getLogger(__name__)

_quote_cache: dict = {}
_quote_fail_cache: dict = {}
_QUOTE_CACHE_SECONDS = 60  # seconds
_QUOTE_FAIL_CACHE_SECONDS = 600  # seconds: remember a symbol Yahoo doesn't have, so a
# watchlist full of delisted/renamed stocks does not re-hit Yahoo every cycle
# (and spam INFO logs) - it is re-checked only after the window expires.


def _clean_company_name(raw_name: str) -> str:
    """Clean raw company names from Yahoo Finance (e.g., 'PGINVIT.NS,0P0001MGQ9...')."""
    if not raw_name:
        return ""
    if "," in raw_name and (".NS" in raw_name or ".BO" in raw_name):
        raw_name = raw_name.split(",")[0]
    return raw_name.removesuffix(".NS").removesuffix(".BO").strip()


def get_quote(exchange: str, symbol: str) -> dict | None:
    """Return {'price', 'prev_close', 'change_pct', 'currency', 'name'} or None."""
    exchange = exchange.upper()
    symbol = symbol.upper().removesuffix(".NS").removesuffix(".BO").strip()
    now = time.time()
    cached = _quote_cache.get((exchange, symbol))
    if cached and now - cached["timestamp"] < _QUOTE_CACHE_SECONDS:
        log.debug("quote cache hit for %s:%s", exchange, symbol)
        return cached["data"]
    failed = _quote_fail_cache.get((exchange, symbol))
    if failed and now - failed < _QUOTE_FAIL_CACHE_SECONDS:
        log.debug("quote negative-cache hit for %s:%s", exchange, symbol)
        return None

    # NSE -> .NS, BSE -> .BO, US -> bare ticker (no Yahoo exchange suffix)
    suffix = ".BO" if exchange == "BSE" else ("" if exchange == "US" else ".NS")
    hosts = [
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    ]
    meta = None
    _throttle_chart_req()
    for host in hosts:
        url = (
            f"{host}/v8/finance/chart/{symbol}{suffix}"
            "?range=1d&interval=1d"
        )
        try:
            response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if "chart" in result and "result" in result["chart"] and result["chart"]["result"]:
                meta = result["chart"]["result"][0]["meta"]
                if meta:
                    break
        except Exception as error:
            log.debug("Quote lookup attempt failed on %s for %s:%s - %s", host, exchange, symbol, error)
            continue

    if not meta:
        # Yahoo has no data for this symbol (delisted, renamed, or a typo).
        # Cache the miss so a stock that will never resolve does not hammer
        # Yahoo every poll cycle and flood the logs with INFO lines.
        _quote_fail_cache[(exchange, symbol)] = now
        log.info("quote lookup failed for %s:%s (Yahoo %s)", exchange, symbol, suffix)
        return None

    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    name = _clean_company_name(meta.get("longName") or meta.get("shortName") or "")
    data = {
        "price": price,
        "prev_close": prev,
        "change_pct": ((price - prev) / prev * 100) if (price is not None and prev) else None,
        "currency": meta.get("currency", "INR"),
        "name": name,
    }
    _quote_cache[(exchange, symbol)] = {"timestamp": now, "data": data}
    log.debug("quote fetched for %s:%s", exchange, symbol)
    return data
