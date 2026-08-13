"""Index constituent universes + per-symbol movement over a time window.

Intraday movement screen. Universe = NSE index constituents from the public
NSE archives CSV (NIFTY 100 by default, NIFTY 500 opt-in) or NASDAQ 100
constituents from the public NASDAQ API (with a static offline fallback).
Per-symbol movement comes from Yahoo bars over the trailing window - US
tickers use a bare Yahoo symbol (no exchange suffix), Indian ones .NS.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime, timezone

from .. import config
from .http import _quote_session, _throttle_chart_req

log = logging.getLogger(__name__)

_universe_cache: dict = {}
_UNIVERSE_CACHE_SECONDS = 86400  # 24h - index constituents change rarely

_INDEX_CSV = {
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}

# NASDAQ 100 constituents from the public NASDAQ API. The endpoint needs
# browser-like headers (it 403s a plain requests UA). A static fallback list
# is embedded so the movers screens still work when the API is unreachable.
_NASDAQ100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
_NASDAQ100_FALLBACK = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALAB", "ALNY", "AMAT",
    "AMD", "AMGN", "AMZN", "APP", "ARM", "ASML", "AVGO", "AXON", "BKR", "BKNG",
    "CCEP", "CDNS", "CEG", "CMCSA", "COST", "CPRT", "CRWD", "CRWV", "CSCO",
    "CSX", "CTAS", "DASH", "DDOG", "DXCM", "EXC", "FANG", "FAST", "FER", "FTNT",
    "GEHC", "GILD", "GOOG", "GOOGL", "HON", "HONA", "IDXX", "INTC", "INTU",
    "ISRG", "KDP", "KHC", "KLAC", "LIN", "LITE", "LRCX", "MAR", "MCHP", "MDLZ",
    "MELI", "META", "MNST", "MPWR", "MRVL", "MSFT", "MSTR", "MU", "NBIS",
    "NFLX", "NVDA", "NXPI", "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PDD",
    "PEP", "PLTR", "PYPL", "QCOM", "REGN", "RKLB", "ROP", "ROST", "SBUX",
    "SHOP", "SNDK", "SNPS", "SPCX", "STX", "TER", "TMUS", "TRI", "TSLA", "TTWO",
    "TXN", "VRTX", "WBD", "WDAY", "WDC", "WMT", "XEL",
]

# S&P 500 constituents from the public GitHub dataset CSV (kept fresh by its
# maintainers). A static fallback list is embedded so the S&P 500 screens still
# work when the CSV host is unreachable.
_SP500_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/master/data/constituents.csv"
)
_SP500_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK-B", "AVGO",
    "JPM", "LLY", "TSLA", "V", "UNH", "XOM", "MA", "PG", "JNJ", "COST", "WMT",
    "HD", "ORCL", "NFLX", "MRK", "CVX", "ABBV", "BAC", "CRM", "KO", "PEP",
    "ADBE", "AMD", "TMO", "LIN", "ACN", "MCD", "PM", "ABT", "CSCO", "QCOM",
    "TXN", "CMCSA", "NEE", "DHR", "AMGN", "GE", "CAT", "ISRG", "VZ", "INTU",
    "IBM", "PFE", "RTX", "DIS", "UBER", "BKNG", "AMAT", "NOW", "SPGI", "GS",
    "AXP", "HON", "MDT", "MS", "BLK", "LOW", "T", "SYK", "TJX", "NKE", "SAP",
    "DE", "UNP", "BA", "SCHW", "COP", "ADI", "GILD", "LMT", "ADP", "BX",
    "FI", "CB", "MMC", "TMUS", "ELV", "CI", "REGN", "TT", "MO", "PLD", "SO",
    "DUK", "APD", "VRTX", "MCO", "ZTS", "CL", "ITW", "WM", "AON", "EQIX",
]

# universe key -> (exchange for Yahoo, is_us flag)
_UNIVERSE_EXCHANGE = {
    "nifty100": "NSE",
    "nifty500": "NSE",
    "nasdaq100": "US",
    "sp500": "US",
}


def _fetch_nasdaq100() -> list[str]:
    """NASDAQ 100 symbols from the NASDAQ API, static list on failure."""
    try:
        response = _quote_session().get(
            _NASDAQ100_URL, timeout=config.HTTP_TIMEOUT,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        response.raise_for_status()
        payload = response.json()
        rows = (payload.get("data") or {}).get("data") or {}
        symbols = [
            (row.get("symbol") or "").strip()
            for row in rows.get("rows") or []
            if (row.get("symbol") or "").strip()
        ]
        if symbols:
            return symbols
        log.info("NASDAQ 100 API returned no rows - using fallback list")
    except Exception as error:
        log.warning("NASDAQ 100 API unavailable (%s) - using fallback list", error)
    return list(_NASDAQ100_FALLBACK)


def _fetch_sp500() -> list[str]:
    """S&P 500 symbols from the public GitHub constituents CSV, fallback list.

    The CSV uses Yahoo's data-source ticker convention for share classes
    (BRK.B) while Yahoo's quote/chart endpoints expect a hyphen (BRK-B), so
    symbols are normalised before returning.
    """
    def _normalize(symbol: str) -> str:
        return symbol.strip().replace(".", "-")

    try:
        response = _quote_session().get(_SP500_URL, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        text = response.text
        if text.startswith("\ufeff"):
            text = text[1:]
        symbols = [
            _normalize(row.get("Symbol") or row.get("symbol") or "")
            for row in csv.DictReader(io.StringIO(text))
            if (row.get("Symbol") or row.get("symbol") or "").strip()
        ]
        symbols = list(dict.fromkeys(symbols))  # dedupe, keep order
        if symbols:
            return symbols
        log.info("S&P 500 CSV returned no rows - using fallback list")
    except Exception as error:
        log.warning("S&P 500 CSV unavailable (%s) - using fallback list", error)
    return [_normalize(symbol) for symbol in _SP500_FALLBACK]


def universe_exchange(index: str) -> str:
    """Yahoo exchange code for a universe key: 'NSE' or 'US'."""
    return _UNIVERSE_EXCHANGE.get((index or "").lower(), "NSE")


def get_index_universe(index: str = "nifty100") -> list[str]:
    """Return symbols for an index universe, cached 24h. Empty list on failure."""
    key = (index or "nifty100").lower()
    if key in ("nasdaq", "nasdaq100", "ndx", "us100", "nasdaq-100"):
        key = "nasdaq100"
    if key == "nasdaq100":
        now = time.time()
        cached = _universe_cache.get("nasdaq100")
        if cached and now - cached["timestamp"] < _UNIVERSE_CACHE_SECONDS:
            log.debug("nasdaq100 universe cache hit (%d symbols)", len(cached["data"]))
            return cached["data"]
        symbols = _fetch_nasdaq100()
        if symbols:
            _universe_cache["nasdaq100"] = {"timestamp": now, "data": symbols}
        return symbols
    if key in ("sp500", "s&p500", "s&p-500", "spx", "us500", "snp500"):
        key = "sp500"
        now = time.time()
        cached = _universe_cache.get("sp500")
        if cached and now - cached["timestamp"] < _UNIVERSE_CACHE_SECONDS:
            log.debug("sp500 universe cache hit (%d symbols)", len(cached["data"]))
            return cached["data"]
        symbols = _fetch_sp500()
        if symbols:
            _universe_cache["sp500"] = {"timestamp": now, "data": symbols}
        return symbols
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
    suffix = "" if exchange.upper() == "US" else (".BO" if exchange.upper() == "BSE" else ".NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=5m"
    )
    data = None
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        closes = (quotes[0] or {}).get("close") or []
        name = meta.get("longName") or meta.get("shortName") or ""
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            data = None
        elif period_minutes <= 0:
            prev = prev_close or (closes[0] if closes else None)
            if prev:
                data = {
                    "price": price,
                    "change_pct": (price / prev - 1) * 100,
                    "change_pct_today": ((price / prev_close - 1) * 100) if prev_close else None,
                    "prev_close": prev_close,
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
                    "change_pct_today": ((price / prev_close - 1) * 100) if prev_close else None,
                    "prev_close": prev_close,
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
    {'price', 'change_pct', 'change_pct_today', 'prev_close', 'days', 'name'}
    or None, where change_pct_today is always the move vs the previous close
    ("today") regardless of the window.
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
    suffix = "" if exchange.upper() == "US" else (".BO" if exchange.upper() == "BSE" else ".NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={range}&interval=1d"
    )
    data = None
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        closes = [close for close in (quotes[0] or {}).get("close") or [] if close is not None]
        if price is None:  # market closed - fall back to the last close
            price = closes[-1] if closes else None
        name = meta.get("longName") or meta.get("shortName") or ""
        # Previous close = the close of the last COMPLETED session. Yahoo's
        # daily bars are timestamped at 03:45 UTC, so a calendar-time cutoff
        # (`now - N days`) lands inside the target day and skips it - the
        # multi-day base must be the close `days` TRADING sessions back.
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if len(closes) >= 2:
            prev_close = closes[-2] or prev_close
        if days <= 1:
            base = prev_close
        elif len(closes) >= days + 1:
            base = closes[-(days + 1)]
        elif len(closes) >= 2:
            base = closes[1]
        else:
            base = prev_close
        if price and base:
            data = {
                "price": price,
                "change_pct": (price / base - 1) * 100,
                "change_pct_today": ((price / prev_close - 1) * 100) if prev_close else None,
                "prev_close": prev_close,
                "days": days,
                "name": name,
            }
    except Exception as error:
        log.info("daily change failed for %s:%s - %s", exchange, symbol, error)
    _daily_cache[key] = {"timestamp": now, "data": data}
    log.debug("daily change fetched for %s:%s (%d days)", exchange, symbol, days)
    return data


_gap_cache: dict = {}
_GAP_CACHE_SECONDS = 60  # seconds - intraday gaps change while the market is open


def get_gap_change(exchange: str, symbol: str) -> dict | None:
    """Today's overnight gap for one symbol (prev close -> today's open).

    Returns {'price', 'prev_close', 'open', 'gap_pct', 'move_from_open_pct',
    'name'} where gap_pct is the %-move from the previous close to today's
    open (the overnight gap) and move_from_open_pct is how far the current
    price has moved since the open. None when the data is unavailable.
    """
    key = (exchange.upper(), symbol.upper())
    now = time.time()
    cached = _gap_cache.get(key)
    if cached and now - cached["timestamp"] < _GAP_CACHE_SECONDS:
        log.debug("gap cache hit for %s:%s", exchange, symbol)
        return cached["data"]
    suffix = "" if exchange.upper() == "US" else (".BO" if exchange.upper() == "BSE" else ".NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=1d"
    )
    data = None
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        price = meta.get("regularMarketPrice")
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        opens = (quotes[0] or {}).get("open") or []
        today_open = next((value for value in reversed(opens) if value is not None), None)
        name = meta.get("longName") or meta.get("shortName") or ""
        if price is not None and today_open is not None and prev_close:
            data = {
                "price": price,
                "prev_close": prev_close,
                "open": today_open,
                "gap_pct": (today_open / prev_close - 1.0) * 100.0,
                "move_from_open_pct": (price / today_open - 1.0) * 100.0,
                "name": name,
            }
    except Exception as error:
        log.info("gap change failed for %s:%s - %s", exchange, symbol, error)
    _gap_cache[key] = {"timestamp": now, "data": data}
    log.debug("gap fetched for %s:%s (%s)", exchange, symbol, "ok" if data else "no data")
    return data


_gap_history_cache: dict = {}
_GAP_HISTORY_CACHE_SECONDS = 300  # seconds


def get_gap_history(exchange: str, symbol: str, days: int = 7) -> list[dict]:
    """Per-day overnight-gap history for one symbol, newest first.

    Each row: {'date', 'open', 'prev_close', 'gap_pct', 'close',
    'move_from_open_pct'}. The gap is the %-move from each session's previous
    close to its own open. Empty list when the data is unavailable.
    """
    key = (exchange.upper(), symbol.upper(), int(days))
    now = time.time()
    cached = _gap_history_cache.get(key)
    if cached and now - cached["timestamp"] < _GAP_HISTORY_CACHE_SECONDS:
        log.debug("gap history cache hit for %s:%s", exchange, symbol)
        return cached["data"]
    if days <= 5:
        range_ = "5d"
    elif days <= 14:
        range_ = "1mo"
    else:
        range_ = "3mo"
    suffix = "" if exchange.upper() == "US" else (".BO" if exchange.upper() == "BSE" else ".NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={range_}&interval=1d"
    )
    rows = []
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        quote = quotes[0] or {}
        opens = quote.get("open") or []
        closes = quote.get("close") or []
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        name = meta.get("longName") or meta.get("shortName") or ""
        for index in range(len(timestamps)):
            open_price = opens[index] if index < len(opens) else None
            close_price = closes[index] if index < len(closes) else None
            if open_price is None or close_price is None:
                continue
            base = prev
            prev = close_price  # next session compares against this close
            if base:
                rows.append({
                    "date": datetime.fromtimestamp(timestamps[index], tz=timezone.utc).date().isoformat(),
                    "open": open_price,
                    "prev_close": base,
                    "gap_pct": (open_price / base - 1.0) * 100.0,
                    "close": close_price,
                    "move_from_open_pct": (close_price / open_price - 1.0) * 100.0,
                    "name": name,
                })
    except Exception as error:
        log.info("gap history failed for %s:%s - %s", exchange, symbol, error)
    rows.reverse()  # newest session first
    _gap_history_cache[key] = {"timestamp": now, "data": rows}
    log.debug("gap history fetched for %s:%s (%d rows)", exchange, symbol, len(rows))
    return rows
