"""Index constituent universes + per-symbol movement over a time window.

Intraday movement screen. Universe = NSE index constituents from the public
NSE archives CSV (NIFTY 100 by default, NIFTY 500 opt-in). Per-symbol movement
comes from Yahoo 5-minute bars over the trailing window.
"""
from __future__ import annotations

import csv
import io
import logging
import time

from .. import config
from .http import _quote_session

log = logging.getLogger(__name__)

_universe_cache: dict = {}
_UNIVERSE_CACHE_SECONDS = 86400  # 24h - index constituents change rarely

_INDEX_CSV = {
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}


def get_index_universe(index: str = "nifty100") -> list[str]:
    """Return symbols for an NSE index, cached 24h. Empty list on failure."""
    key = (index or "nifty100").lower()
    url = _INDEX_CSV["nifty500"] if key in ("all", "500", "nifty500") else _INDEX_CSV["nifty100"]
    now = time.time()
    cached = _universe_cache.get(url)
    if cached and now - cached["timestamp"] < _UNIVERSE_CACHE_SECONDS:
        log.debug("index universe cache hit for %s (%d symbols)", key, len(cached["data"]))
        return cached["data"]
    symbols = []
    try:
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        text = response.text
        if text.startswith("\ufeff"):
            text = text[1:]
        for row in csv.DictReader(io.StringIO(text)):
            symbol = (row.get("Symbol") or "").strip()
            if symbol:
                symbols.append(symbol)
        log.info("index universe %s loaded fresh: %d symbols", key, len(symbols))
    except Exception as error:
        log.warning("NSE index universe unavailable (%s): %s", index, error)
        symbols = []
    # Only cache a successful (non-empty) load. Caching an empty list for 24h
    # after one transient failure would silently make /movers and /harmonic
    # scans return nothing for the rest of the day.
    if symbols:
        _universe_cache[url] = {"timestamp": now, "data": symbols}
    return symbols


_intraday_cache: dict = {}
_INTRADAY_CACHE_SECONDS = 60  # seconds


def get_intraday_change(exchange: str, symbol: str, period_minutes: int) -> dict | None:
    """% move over the trailing window using Yahoo 5-minute bars, cached.

    period_minutes <= 0 means "today" (vs the previous close). Returns
    {'price', 'change_pct', 'period_minutes', 'name'} or None.
    """
    key = (exchange.upper(), symbol.upper(), int(period_minutes))
    now = time.time()
    cached = _intraday_cache.get(key)
    if cached and now - cached["timestamp"] < _INTRADAY_CACHE_SECONDS:
        log.debug("intraday cache hit for %s:%s", exchange, symbol)
        return cached["data"]
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=5m"
    )
    data = None
    try:
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        closes = (quotes[0] or {}).get("close") or []
        name = meta.get("longName") or meta.get("shortName") or ""
        if price is None:
            data = None
        elif period_minutes <= 0:
            prev = (
                meta.get("chartPreviousClose")
                or meta.get("previousClose")
                or (closes[0] if closes else None)
            )
            if prev:
                data = {
                    "price": price,
                    "change_pct": (price / prev - 1) * 100,
                    "period_minutes": 0,
                    "name": name,
                }
        else:
            cutoff = now - period_minutes * 60
            base = None
            for timestamp, close in zip(timestamps, closes):
                if close is None:
                    continue
                if timestamp >= cutoff:
                    base = close
                    break
            if base is None:
                base = closes[0] if closes else None
            if base:
                data = {
                    "price": price,
                    "change_pct": (price / base - 1) * 100,
                    "period_minutes": period_minutes,
                    "name": name,
                }
    except Exception as error:
        log.info("intraday change failed for %s:%s - %s", exchange, symbol, error)
    _intraday_cache[key] = {"timestamp": now, "data": data}
    log.debug("intraday change fetched for %s:%s (%s)", exchange, symbol, "ok" if data else "no data")
    return data


_daily_cache: dict = {}
_DAILY_CACHE_SECONDS = 300  # seconds - daily moves change slowly


def get_daily_change(exchange: str, symbol: str, days: int) -> dict | None:
    """% move over the trailing N-day window using Yahoo daily bars, cached.

    days=1 means vs the previous close ("today"). Returns
    {'price', 'change_pct', 'days', 'name'} or None.
    """
    days = max(1, int(days))
    key = (exchange.upper(), symbol.upper(), "d", days)
    now = time.time()
    cached = _daily_cache.get(key)
    if cached and now - cached["timestamp"] < _DAILY_CACHE_SECONDS:
        log.debug("daily change cache hit for %s:%s (%d days)", exchange, symbol, days)
        return cached["data"]
    if days <= 1:
        range = "1d"
    elif days <= 5:
        range = "5d"
    elif days <= 30:
        range = "1mo"
    elif days <= 90:
        range = "3mo"
    elif days <= 180:
        range = "6mo"
    else:
        range = "1y"
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={range}&interval=1d"
    )
    data = None
    try:
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        closes = (quotes[0] or {}).get("close") or []
        if price is None:  # market closed - fall back to the last close
            for close in reversed(closes):
                if close is not None:
                    price = close
                    break
        name = meta.get("longName") or meta.get("shortName") or ""
        if days <= 1:
            base = meta.get("chartPreviousClose") or meta.get("previousClose")
        else:
            cutoff = now - days * 86400
            base = None
            for timestamp, close in zip(timestamps, closes):
                if close is None:
                    continue
                if timestamp >= cutoff:
                    base = close
                    break
            if base is None:
                base = next((close for close in closes if close is not None), None)
        if price and base:
            data = {
                "price": price,
                "change_pct": (price / base - 1) * 100,
                "days": days,
                "name": name,
            }
    except Exception as error:
        log.info("daily change failed for %s:%s - %s", exchange, symbol, error)
    _daily_cache[key] = {"timestamp": now, "data": data}
    log.debug("daily change fetched for %s:%s (%d days)", exchange, symbol, days)
    return data
