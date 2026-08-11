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
_UNIVERSE_TTL = 86400  # 24h - index constituents change rarely

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
    if cached and now - cached["ts"] < _UNIVERSE_TTL:
        log.debug("index universe cache hit for %s (%d symbols)", key, len(cached["data"]))
        return cached["data"]
    symbols = []
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        if text.startswith("\ufeff"):
            text = text[1:]
        for row in csv.DictReader(io.StringIO(text)):
            sym = (row.get("Symbol") or "").strip()
            if sym:
                symbols.append(sym)
        log.info("index universe %s loaded fresh: %d symbols", key, len(symbols))
    except Exception as exc:
        log.warning("NSE index universe unavailable (%s): %s", index, exc)
        symbols = []
    # Only cache a successful (non-empty) load. Caching an empty list for 24h
    # after one transient failure would silently make /movers and /harmonic
    # scans return nothing for the rest of the day.
    if symbols:
        _universe_cache[url] = {"ts": now, "data": symbols}
    return symbols


_intraday_cache: dict = {}
_INTRADAY_TTL = 60  # seconds


def get_intraday_change(exchange: str, symbol: str, period_minutes: int) -> dict | None:
    """% move over the trailing window using Yahoo 5-minute bars, cached.

    period_minutes <= 0 means "today" (vs the previous close). Returns
    {'price', 'change_pct', 'period_minutes', 'name'} or None.
    """
    key = (exchange.upper(), symbol.upper(), int(period_minutes))
    now = time.time()
    cached = _intraday_cache.get(key)
    if cached and now - cached["ts"] < _INTRADAY_TTL:
        log.debug("intraday cache hit for %s:%s", exchange, symbol)
        return cached["data"]
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=5m"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        price = meta.get("regularMarketPrice")
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
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
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                if t >= cutoff:
                    base = c
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
    except Exception as exc:
        log.info("intraday change failed for %s:%s - %s", exchange, symbol, exc)
    _intraday_cache[key] = {"ts": now, "data": data}
    log.debug("intraday change fetched for %s:%s (%s)", exchange, symbol, "ok" if data else "no data")
    return data


_daily_cache: dict = {}
_DAILY_TTL = 300  # seconds - daily moves change slowly


def get_daily_change(exchange: str, symbol: str, days: int) -> dict | None:
    """% move over the trailing N-day window using Yahoo daily bars, cached.

    days=1 means vs the previous close ("today"). Returns
    {'price', 'change_pct', 'days', 'name'} or None.
    """
    days = max(1, int(days))
    key = (exchange.upper(), symbol.upper(), "d", days)
    now = time.time()
    cached = _daily_cache.get(key)
    if cached and now - cached["ts"] < _DAILY_TTL:
        log.debug("daily change cache hit for %s:%s (%d days)", exchange, symbol, days)
        return cached["data"]
    if days <= 1:
        rng = "1d"
    elif days <= 5:
        rng = "5d"
    elif days <= 30:
        rng = "1mo"
    elif days <= 90:
        rng = "3mo"
    elif days <= 180:
        rng = "6mo"
    else:
        rng = "1y"
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={rng}&interval=1d"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        price = meta.get("regularMarketPrice")
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
        closes = (quotes[0] or {}).get("close") or []
        if price is None:  # market closed - fall back to the last close
            for c in reversed(closes):
                if c is not None:
                    price = c
                    break
        name = meta.get("longName") or meta.get("shortName") or ""
        if days <= 1:
            base = meta.get("chartPreviousClose") or meta.get("previousClose")
        else:
            cutoff = now - days * 86400
            base = None
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                if t >= cutoff:
                    base = c
                    break
            if base is None:
                base = next((c for c in closes if c is not None), None)
        if price and base:
            data = {
                "price": price,
                "change_pct": (price / base - 1) * 100,
                "days": days,
                "name": name,
            }
    except Exception as exc:
        log.info("daily change failed for %s:%s - %s", exchange, symbol, exc)
    _daily_cache[key] = {"ts": now, "data": data}
    log.debug("daily change fetched for %s:%s (%d days)", exchange, symbol, days)
    return data
