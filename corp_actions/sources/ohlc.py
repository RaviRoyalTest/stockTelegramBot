"""Raw candlestick series for a symbol/timeframe (used by the scanners).

Yahoo chart endpoint, cached a short while.
"""
from __future__ import annotations

import logging
import time

from .. import config
from .http import _quote_session

log = logging.getLogger(__name__)

_ohlc_cache: dict = {}
_OHLC_TTL = 120  # seconds

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

_HFT_LADDER = {
    "5m": "15m",
    "15m": "1h",
    "30m": "4h",
    "1h": "4h",
    "4h": "1d",
    "1d": "1w",
    "1w": "1mo",
}


def _bars_from_response(res: dict, name: str, exchange: str, symbol: str,
                        interval: str, timeframe: str) -> dict | None:
    """Build the aligned-bar dict from a Yahoo chart response."""
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    quotes = (res.get("indicators") or {}).get("quote") or [{}]
    q = quotes[0] or {}
    opens, highs, lows, closes, vols = q.get("open") or [], q.get("high") or [], \
        q.get("low") or [], q.get("close") or [], q.get("volume") or []
    rows = []
    for i in range(len(ts)):
        if i >= len(opens) or i >= len(highs) or i >= len(lows) or i >= len(closes):
            break
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        rows.append((ts[i], o, h, l, c, v))
    if not rows:
        return None
    return {
        "timestamp": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
        "volume": [r[5] for r in rows],
        "interval": interval,
        "timeframe": timeframe,
        "name": name,
        "exchange": exchange.upper(),
        "symbol": symbol.upper(),
    }


def get_ohlc(exchange: str, symbol: str, timeframe: str = "1d") -> dict | None:
    """Return OHLC bars for a symbol/timeframe via Yahoo chart, cached.

    Returns {'timestamp','open','high','low','close','volume','interval',
    'name','exchange','symbol','timeframe'} with the arrays aligned to the
    same bars, or None on any failure. Incomplete leading/trailing bars are
    dropped.
    """
    timeframe = (timeframe or "1d").lower()
    if timeframe not in OHLC_TIMEFRAMES:
        log.info("ohlc: unknown timeframe %r for %s:%s — returning None", timeframe, exchange, symbol)
        return None
    interval, rng = OHLC_TIMEFRAMES[timeframe]
    key = (exchange.upper(), symbol.upper(), interval)
    now = time.time()
    cached = _ohlc_cache.get(key)
    if cached and now - cached["ts"] < _OHLC_TTL:
        log.debug("ohlc cache hit for %s:%s (%s)", exchange, symbol, interval)
        return cached["data"]
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={rng}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        name = meta.get("longName") or meta.get("shortName") or symbol
        data = _bars_from_response(res, name, exchange, symbol, interval, timeframe)
        if data:
            log.info("ohlc: %d %s bars for %s:%s", len(data["timestamp"]), interval, exchange, symbol)
    except Exception as exc:
        log.info("ohlc failed for %s:%s (%s) - %s", exchange, symbol, interval, exc)
    _ohlc_cache[key] = {"ts": now, "data": data}
    return data


_index_ohlc_cache: dict = {}
_INDEX_OHLC_TTL = 180  # seconds


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
    if cached and now - cached["ts"] < _INDEX_OHLC_TTL:
        log.debug("index ohlc cache hit for %s", index_symbol)
        return cached["data"]
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{index_symbol}"
        f"?range={range_}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        name = meta.get("longName") or meta.get("shortName") or index_symbol
        data = _bars_from_response(res, name, "IDX", index_symbol, interval, interval)
        if data:
            log.info("index ohlc: %d %s bars for %s", len(data["timestamp"]), interval, index_symbol)
    except Exception as exc:
        log.info("index ohlc failed for %s - %s", index_symbol, exc)
    _index_ohlc_cache[key] = {"ts": now, "data": data}
    return data
