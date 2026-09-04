"""Live quotes from Yahoo Finance (public, no key)."""
from __future__ import annotations

import logging
import time
import threading

from .. import config
from .http import _quote_session, _throttle_chart_req
from .http import _throttle_chart_req_async, _async_client
import asyncio

log = logging.getLogger(__name__)

_quote_cache: dict = {}
_quote_fail_cache: dict = {}
_QUOTE_CACHE_SECONDS = 60  # seconds
_QUOTE_FAIL_CACHE_SECONDS = 600  # seconds: remember a symbol Yahoo doesn't have, so a
# watchlist full of delisted/renamed stocks does not re-hit Yahoo every cycle
# (and spam INFO logs) - it is re-checked only after the window expires.

# Local Yahoo circuit-breaker to avoid repeated 429s/failures
_yahoo_lock = threading.Lock()
_yahoo_fail_count = 0
_yahoo_blocked_until = 0.0
_YAHOO_MAX_FAILS = 5
_YAHOO_BLOCK_SECONDS = 300


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
    now = time.time()
    with _yahoo_lock:
        if now < _yahoo_blocked_until:
            log.info("quote lookup: Yahoo temporarily blocked until %s", _yahoo_blocked_until)
            return None
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
            # treat repeated failures/429s as potential rate-limit and update circuit-breaker
            try:
                status = getattr(error, 'response', None)
                if status is not None and getattr(status, 'status_code', None) == 429:
                    with _yahoo_lock:
                        _yahoo_fail_count += 1
                        if _yahoo_fail_count >= _YAHOO_MAX_FAILS:
                            _yahoo_blocked_until = time.time() + _YAHOO_BLOCK_SECONDS
                            _yahoo_fail_count = 0
                            log.warning("Yahoo appears rate-limited - pausing Yahoo calls for %s seconds", _YAHOO_BLOCK_SECONDS)
            except Exception:
                pass
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


async def get_quote_async(exchange: str, symbol: str) -> dict | None:
    """Async equivalent of `get_quote` using httpx when available.

    Falls back to running the sync `get_quote` in a thread if `httpx` is
    unavailable.
    """
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

    suffix = ".BO" if exchange == "BSE" else ("" if exchange == "US" else ".NS")
    hosts = [
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    ]
    meta = None
    # Use async throttle when available
    try:
        await _throttle_chart_req_async()
    except Exception:
        pass

    client = _async_client()
    if client is None:
        # fallback to sync in thread
        return await asyncio.to_thread(get_quote, exchange, symbol)

    now = time.time()
    with _yahoo_lock:
        if now < _yahoo_blocked_until:
            log.info("async quote lookup: Yahoo temporarily blocked until %s", _yahoo_blocked_until)
            return None

    for host in hosts:
        url = (
            f"{host}/v8/finance/chart/{symbol}{suffix}"
            "?range=1d&interval=1d"
        )
        try:
            r = await client.get(url, timeout=config.HTTP_TIMEOUT)
            r.raise_for_status()
            result = r.json()
            if "chart" in result and "result" in result["chart"] and result["chart"]["result"]:
                meta = result["chart"]["result"][0]["meta"]
                if meta:
                    break
        except Exception as error:
            log.debug("Async quote lookup failed on %s for %s:%s - %s", host, exchange, symbol, error)
            # handle 429/exceptions for circuit-breaker
            try:
                status = getattr(error, 'response', None)
                if status is not None and getattr(status, 'status_code', None) == 429:
                    with _yahoo_lock:
                        _yahoo_fail_count += 1
                        if _yahoo_fail_count >= _YAHOO_MAX_FAILS:
                            _yahoo_blocked_until = time.time() + _YAHOO_BLOCK_SECONDS
                            _yahoo_fail_count = 0
                            log.warning("Yahoo async appears rate-limited - pausing Yahoo calls for %s seconds", _YAHOO_BLOCK_SECONDS)
            except Exception:
                pass
            continue

    if not meta:
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
    log.debug("async quote fetched for %s:%s", exchange, symbol)
    return data
