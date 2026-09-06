"""Raw candlestick series for a symbol/timeframe (used by the scanners).

Yahoo chart endpoint, cached a short while.
"""
from __future__ import annotations

import logging
import time

from .. import config
from .http import _quote_session, _throttle_chart_req

log = logging.getLogger(__name__)

_ohlc_cache: dict = {}
_OHLC_CACHE_SECONDS = 120  # seconds

OHLC_TIMEFRAMES = {
    "5m": ("5m", "1d"),
    "15m": ("15m", "5d"),
    "30m": ("30m", "1mo"),
    "1h": ("1h", "3mo"),
    "4h": ("4h", "6mo"),
    "1d": ("1d", "2y"),
    "1w": ("1wk", "5y"),
    "1mo": ("1mo", "10y"),
}

_HIGHER_TIMEFRAME_LADDER = {
    "5m": "15m",
    "15m": "1h",
    "30m": "4h",
    "1h": "4h",
    "4h": "1d",
    "1d": "1w",
    "1w": "1mo",
}


def _bars_from_response(result: dict, name: str, exchange: str, symbol: str,
                        interval: str, timeframe: str) -> dict | None:
    """Build the aligned-bar dict from a Yahoo chart response."""
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quotes[0] or {}
    opens, highs, lows, closes, vols = quote.get("open") or [], quote.get("high") or [], \
        quote.get("low") or [], quote.get("close") or [], quote.get("volume") or []
    rows = []
    for index in range(len(timestamps)):
        if index >= len(opens) or index >= len(highs) or index >= len(lows) or index >= len(closes):
            break
        open_price, high_price, low_price, close_price = opens[index], highs[index], lows[index], closes[index]
        if None in (open_price, high_price, low_price, close_price):
            continue
        volume = vols[index] if index < len(vols) and vols[index] is not None else 0
        rows.append((timestamps[index], open_price, high_price, low_price, close_price, volume))
    if not rows:
        return None
    return {
        "timestamp": [row[0] for row in rows],
        "open": [row[1] for row in rows],
        "high": [row[2] for row in rows],
        "low": [row[3] for row in rows],
        "close": [row[4] for row in rows],
        "volume": [row[5] for row in rows],
        "interval": interval,
        "timeframe": timeframe,
        "name": name,
        "exchange": exchange.upper(),
        "symbol": symbol.upper(),
    }


def get_ohlc(exchange: str, symbol: str, timeframe: str = "1d") -> dict | None:
    """Return OHLC bars for a symbol/timeframe via Yahoo chart, cached.

    exchange picks the Yahoo suffix: 'NSE' -> .NS, 'BSE' -> .BO, 'US' -> bare
    ticker (NASDAQ/NYSE). Returns {'timestamp','open','high','low','close',
    'volume','interval','name','exchange','symbol','timeframe'} with the
    arrays aligned to the same bars, or None on any failure. Incomplete
    leading/trailing bars are dropped.
    """
    timeframe = (timeframe or "1d").lower()
    if timeframe not in OHLC_TIMEFRAMES:
        log.info("ohlc: unknown timeframe %r for %s:%s — returning None", timeframe, exchange, symbol)
        return None
    interval, range = OHLC_TIMEFRAMES[timeframe]
    key = (exchange.upper(), symbol.upper(), interval)
    now = time.time()
    cached = _ohlc_cache.get(key)
    if cached and now - cached["timestamp"] < _OHLC_CACHE_SECONDS:
        log.debug("ohlc cache hit for %s:%s (%s)", exchange, symbol, interval)
        return cached["data"]
    # 'US' -> bare ticker (NASDAQ/NYSE), 'BSE' -> .BO, anything else (NSE) -> .NS
    suffix = "" if exchange.upper() == "US" else (".BO" if exchange.upper() == "BSE" else ".NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={range}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        name = meta.get("longName") or meta.get("shortName") or symbol
        data = _bars_from_response(result, name, exchange, symbol, interval, timeframe)
        if data:
            log.info("ohlc: %d %s bars for %s:%s", len(data["timestamp"]), interval, exchange, symbol)
    except Exception as error:
        log.info("ohlc failed for %s:%s (%s) - %s", exchange, symbol, interval, error)
    _ohlc_cache[key] = {"timestamp": now, "data": data}
    return data


_index_ohlc_cache: dict = {}
_INDEX_OHLC_CACHE_SECONDS = 180  # seconds


_chart_range_cache: dict = {}
_CHART_RANGE_CACHE_SECONDS = 120  # seconds
_CHART_RANGE_FAIL_SECONDS = 15   # don't pin transient failures for 2 minutes

# Chart-friendly range/interval combos (Groww-style toolbar).
# Keys are 'range' values served by /api/history; values are
# (yahoo_interval, yahoo_range) pairs.
CHART_RANGES = {
    "1d": ("5m", "1d"),
    "5d": ("15m", "5d"),
    "1mo": ("1h", "1mo"),
    "6mo": ("1d", "6mo"),
    "1y": ("1d", "1y"),
    "5y": ("1wk", "5y"),
    "max": ("1mo", "max"),
}


def get_chart_ohlc(exchange: str, symbol: str, range_key: str) -> dict | None:
    """OHLC bars for a dashboard-chart range (e.g. '1y' = daily bars, 1y).

    Unlike get_ohlc (scanner timeframes that couple interval to a fixed
    lookback), this takes explicit chart ranges so '1mo' means one month of
    hourly bars instead of ten years of monthly candles. Same return shape
    and caching pattern as get_ohlc.
    """
    range_key = (range_key or "1d").lower()
    combo = CHART_RANGES.get(range_key)
    if not combo:
        log.info("chart ohlc: unknown range %r for %s:%s", range_key, exchange, symbol)
        return None
    interval, range_ = combo
    key = (exchange.upper(), symbol.upper(), interval, range_)
    now = time.time()
    cached = _chart_range_cache.get(key)
    if cached:
        # A failed fetch (None) is only trusted briefly so a transient Yahoo
        # hiccup doesn't blank the chart for two full minutes; successes
        # cache for the normal TTL.
        ttl = _CHART_RANGE_CACHE_SECONDS if cached["data"] else _CHART_RANGE_FAIL_SECONDS
        if now - cached["timestamp"] < ttl:
            return cached["data"]
    suffix = "" if exchange.upper() == "US" else (".BO" if exchange.upper() == "BSE" else ".NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={range_}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        name = meta.get("longName") or meta.get("shortName") or symbol
        data = _bars_from_response(result, name, exchange, symbol, interval, range_)
        if data:
            log.info("chart ohlc: %d %s/%s bars for %s:%s", len(data["timestamp"]), interval, range_, exchange, symbol)
    except Exception as error:
        log.info("chart ohlc failed for %s:%s (%s/%s) - %s", exchange, symbol, interval, range_, error)
    _chart_range_cache[key] = {"timestamp": now, "data": data}
    return data


def get_index_ohlc(index_symbol: str, range_: str = "6mo",
                   interval: str = "1d") -> dict | None:
    """Return OHLC bars for a Yahoo index symbol (e.g. ^NSEI, ^INDIAVIX).

    Index symbols carry no exchange suffix, so this bypasses the .NS/.BO
    logic in get_ohlc. Same dict shape as get_ohlc. Cached briefly.
    """
    index_symbol = (index_symbol or "").strip()
    if not index_symbol:
        return None
    key = (index_symbol.upper(), range_, interval)
    now = time.time()
    cached = _index_ohlc_cache.get(key)
    if cached and now - cached["timestamp"] < _INDEX_OHLC_CACHE_SECONDS:
        log.debug("index ohlc cache hit for %s", index_symbol)
        return cached["data"]
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{index_symbol}"
        f"?range={range_}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        name = meta.get("longName") or meta.get("shortName") or index_symbol
        data = _bars_from_response(result, name, "IDX", index_symbol, interval, interval)
        if data:
            log.info("index ohlc: %d %s bars for %s", len(data["timestamp"]), interval, index_symbol)
    except Exception as error:
        log.info("index ohlc failed for %s - %s", index_symbol, error)
    _index_ohlc_cache[key] = {"timestamp": now, "data": data}
    return data
