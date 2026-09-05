"""Free (no-key) quote + history fallbacks and fundamentals normalisation.

All sources here work without an API key:

  * Stooq CSV  - https://stooq.com/q/l/?s=<sym>.ns&f=sd2t2ohlcv&h&e=csv
                 intraday quote (price/open/high/low/volume, no crumb).
  * Stooq daily - https://stooq.com/q/d/l/?s=<sym>.ns&i=d
                 daily OHLC history for the price chart fallback.
  * NSE India  - https://www.nseindia.com/api/quote-equity?symbol=<SYM>
                 lastPrice/previousClose/52-week/company name (cookie dance).

Plus `normalise_fundamentals`, which adds the alias keys the screener and
the web UI expect so no tab ever shows a blank because Yahoo called the
field `rsi` while the screener looked for `rsi14`:

  market_cap  <- mcap_cr | market_cap
  rsi14       <- rsi | rsi14
  macd_bull   <- macd_hist > 0 | macd_line > macd_signal
  above_ema200<- price >= sma_200
  company     <- company | name | longName
  price       <- quote price when fund price is missing
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time

from .. import config
from .http import _quote_session

log = logging.getLogger(__name__)

_STOOQ_CACHE: dict = {}
_STOOQ_TTL = 120  # seconds - intraday quotes move fast
_NSE_CACHE: dict = {}
_NSE_TTL = 120
_NSE_COOKIES: dict = {}
_NSE_COOKIE_TS = 0.0
_NSE_COOKIE_TTL = 600  # refresh NSE cookies every 10 min


def _safe_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(str(value).replace(",", "").strip())
        if out != out or out in (float("inf"), float("-inf")):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _stooq_symbol(symbol: str, exchange: str) -> str:
    base = (symbol or "").strip().upper().removesuffix(".NS").removesuffix(".BO")
    ex = (exchange or "NSE").upper()
    if ex == "BSE":
        return f"{base.lower()}.bo"
    if ex in ("US", "NASDAQ", "NYSE"):
        return f"{base.lower()}.us"
    return f"{base.lower()}.ns"


# ---------------------------------------------------------------- Stooq quote

def get_stooq_quote(symbol: str, exchange: str = "NSE") -> dict | None:
    """Live quote from Stooq's free CSV endpoint (no key, no crumb).

    Returns {'price','open','high','low','prev_close','change_pct','volume',
    'source'} or None. Stooq's one-row CSV has no previous close, so the
    day change is measured vs the open (intraday proxy) when needed.
    """
    key = (exchange.upper(), symbol.strip().upper())
    now = time.time()
    cached = _STOOQ_CACHE.get(key)
    if cached and now - cached["timestamp"] < _STOOQ_TTL:
        return cached["data"]
    data: dict | None = None
    try:
        stooq = _stooq_symbol(symbol, exchange)
        url = f"https://stooq.com/q/l/?s={stooq}&f=sd2t2ohlcv&h&e=csv"
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if rows:
            row = rows[0]
            close = _safe_float(row.get("Close"))
            open_ = _safe_float(row.get("Open"))
            high = _safe_float(row.get("High"))
            low = _safe_float(row.get("Low"))
            vol = _safe_float(row.get("Volume"))
            if close:
                change = None
                if open_:
                    change = round((close / open_ - 1.0) * 100, 2)
                data = {
                    "price": close,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "prev_close": None,
                    "change_pct": change,
                    "volume": int(vol) if vol is not None else None,
                    "currency": "INR" if exchange.upper() != "US" else "USD",
                    "name": "",
                    "source": "stooq",
                }
    except Exception as error:
        log.info("stooq quote failed for %s:%s - %s", exchange, symbol, error)
        data = None
    _STOOQ_CACHE[key] = {"timestamp": now, "data": data}
    return data


def get_stooq_history(symbol: str, exchange: str = "NSE") -> dict | None:
    """Daily OHLC history from Stooq's free daily CSV (for the price chart).

    Returns the same shape as sources.get_ohlc (timestamp/open/high/low/
    close/volume/name/exchange/symbol) or None.
    """
    try:
        stooq = _stooq_symbol(symbol, exchange)
        url = f"https://stooq.com/q/d/l/?s={stooq}&i=d"
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        ts, opens, highs, lows, closes, vols = [], [], [], [], [], []
        for row in rows[-260:]:  # last ~1y of trading days
            try:
                from datetime import datetime, timezone

                day = datetime.strptime(row["Date"].strip(), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except Exception:
                continue
            try:
                o, h, lo, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
            except Exception:
                continue
            try:
                v = int(float(row.get("Volume") or 0))
            except Exception:
                v = 0
            ts.append(int(day.timestamp()))
            opens.append(o)
            highs.append(h)
            lows.append(lo)
            closes.append(c)
            vols.append(v)
        if not ts:
            return None
        return {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
            "interval": "1d",
            "timeframe": "1d",
            "name": symbol.upper(),
            "exchange": exchange.upper(),
            "symbol": symbol.upper(),
            "source": "stooq",
        }
    except Exception as error:
        log.info("stooq history failed for %s:%s - %s", exchange, symbol, error)
        return None


# ---------------------------------------------------------------- NSE quote

def _nse_cookies() -> dict:
    """Refreshable NSE cookie jar (the quote API 401s without a prior visit)."""
    global _NSE_COOKIES, _NSE_COOKIE_TS
    now = time.time()
    if _NSE_COOKIES and now - _NSE_COOKIE_TS < _NSE_COOKIE_TTL:
        return dict(_NSE_COOKIES)
    try:
        sess = _quote_session()
        sess.get("https://www.nseindia.com", timeout=config.HTTP_TIMEOUT)
        _NSE_COOKIES = {c.name: c.value for c in sess.cookies}
        _NSE_COOKIE_TS = now
    except Exception as error:
        log.info("nse cookie refresh failed: %s", error)
    return dict(_NSE_COOKIES)


def get_nse_quote(symbol: str) -> dict | None:
    """Quote from NSE's free quote-equity API (no key, cookie-guarded).

    Returns {'price','prev_close','change_pct','open','high','low',
    'wk52_high','wk52_low','pe','company','source'} or None.
    """
    base = (symbol or "").strip().upper().removesuffix(".NS").removesuffix(".BO")
    if not base:
        return None
    now = time.time()
    cached = _NSE_CACHE.get(base)
    if cached and now - cached["timestamp"] < _NSE_TTL:
        return cached["data"]
    data: dict | None = None
    try:
        cookies = _nse_cookies()
        resp = _quote_session().get(
            f"https://www.nseindia.com/api/quote-equity?symbol={base}",
            timeout=config.HTTP_TIMEOUT,
            cookies=cookies,
            headers={"Accept": "application/json, text/plain, */*"},
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        info = payload.get("info") or {}
        meta = payload.get("metadata") or {}
        price_info = payload.get("priceInfo") or {}
        price = _safe_float(price_info.get("lastPrice"))
        prev = _safe_float(price_info.get("previousClose"))
        change = _safe_float(price_info.get("pChange"))
        if price is None:
            price = _safe_float(meta.get("lastPrice"))
        company = (info.get("companyName") or meta.get("companyName") or "").strip()
        pe_raw = meta.get("pdSymbolPe") or meta.get("pe")
        pe = _safe_float(pe_raw)
        wk52_high = _safe_float((payload.get("priceInfo") or {}).get("weekHighLow", {}).get("max")) \
            or _safe_float(meta.get("weekHighLow", {}).get("max") if isinstance(meta.get("weekHighLow"), dict) else None)
        wk52_low = _safe_float((payload.get("priceInfo") or {}).get("weekHighLow", {}).get("min")) \
            or _safe_float(meta.get("weekHighLow", {}).get("min") if isinstance(meta.get("weekHighLow"), dict) else None)
        # NSE nests 52w inside priceInfo.weekHighLow.{max,min} on most builds.
        week = price_info.get("weekHighLow") or {}
        if wk52_high is None:
            wk52_high = _safe_float(week.get("max"))
        if wk52_low is None:
            wk52_low = _safe_float(week.get("min"))
        if price is not None:
            if change is None and prev:
                change = round((price / prev - 1.0) * 100, 2)
            data = {
                "price": price,
                "prev_close": prev,
                "change_pct": change,
                "open": _safe_float(price_info.get("open")),
                "high": _safe_float(price_info.get("intraDayHighLow", {}).get("max") if isinstance(price_info.get("intraDayHighLow"), dict) else None),
                "low": _safe_float(price_info.get("intraDayHighLow", {}).get("min") if isinstance(price_info.get("intraDayHighLow"), dict) else None),
                "wk52_high": wk52_high,
                "wk52_low": wk52_low,
                "pe": pe,
                "company": company,
                "currency": "INR",
                "name": company,
                "source": "nse",
            }
    except Exception as error:
        log.info("nse quote failed for %s - %s", base, error)
        data = None
    _NSE_CACHE[base] = {"timestamp": now, "data": data}
    return data


# ---------------------------------------------------------------- best quote

def get_best_quote(exchange: str, symbol: str, yahoo_quote: dict | None = None) -> dict:
    """Merge Yahoo + NSE + Stooq quotes so the UI never shows a blank price.

    Priority: Yahoo (has prev-close change%) -> NSE (has company + 52w) ->
    Stooq (always reachable). Missing scalar fields are back-filled from the
    lower-priority sources and the winning `source` plus a `sources_tried`
    list are attached for the UI's data-source badge.
    """
    tried: list[str] = []
    merged: dict = {}
    # Lazy import to avoid a circulars import at module load.
    if yahoo_quote is None:
        try:
            from .quotes import get_quote as _yahoo_quote

            yahoo_quote = _yahoo_quote(exchange, symbol)
        except Exception:
            yahoo_quote = None
    if yahoo_quote:
        merged.update({k: v for k, v in yahoo_quote.items() if v is not None})
        merged["source"] = yahoo_quote.get("source") or "yahoo"
        tried.append("yahoo")
    nse: dict | None = None
    if exchange.upper() in ("NSE", "BSE", ""):
        try:
            nse = get_nse_quote(symbol)
        except Exception:
            nse = None
        if nse:
            tried.append("nse")
            for key, value in nse.items():
                if value is not None and merged.get(key) is None:
                    merged[key] = value
            if "source" not in merged:
                merged["source"] = "nse"
            # NSE company names are authoritative ("Reliance Industries Ltd").
            if nse.get("company") and not merged.get("name"):
                merged["name"] = nse["company"]
    stooq: dict | None = None
    if merged.get("price") is None:
        try:
            stooq = get_stooq_quote(symbol, exchange)
        except Exception:
            stooq = None
        if stooq:
            tried.append("stooq")
            for key, value in stooq.items():
                if value is not None and merged.get(key) is None:
                    merged[key] = value
            if "source" not in merged:
                merged["source"] = "stooq"
    else:
        tried.append("yahoo-hit")
    if tried and "yahoo" not in tried and yahoo_quote:
        tried.insert(0, "yahoo")
    merged["sources_tried"] = tried or ["yahoo", "nse", "stooq"]
    if "source" not in merged:
        merged["source"] = "none"
    return merged


# ---------------------------------------------------------------- normalise

def _parse_pct_string(value) -> float | None:
    """Parse screener-style '50.2% (🟢 +0.48%)' holding strings to a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"([\d.,]+)\s*%", str(value))
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return _safe_float(str(value).split()[0] if str(value).strip() else None)


def normalise_fundamentals(symbol: str, fund: dict, quote: dict | None = None) -> dict:
    """Add UI/screener alias keys in place and return the same dict.

    Never drops the original keys - it only ADDS the aliases every consumer
    expects, so old and new callers keep working:

      company <- company | name | quote.name
      market_cap (₹ Cr) <- market_cap | mcap_cr
      rsi14 <- rsi | rsi14
      macd_bull <- macd_hist>0 | macd_line>macd_signal
      above_ema200 / above_sma200 <- price >= sma_200
      price <- fund.price | quote.price
      promoter/fii/dii/public numeric twins (promoter_pct_num ...)
    """
    quote = quote or {}
    # -- company name ------------------------------------------------------
    company = (
        fund.get("company")
        or fund.get("name")
        or quote.get("name")
        or quote.get("company")
        or symbol.strip().upper()
    )
    fund["company"] = company
    fund.setdefault("name", company)
    # -- price --------------------------------------------------------------
    if fund.get("price") is None and quote.get("price") is not None:
        fund["price"] = quote["price"]
    price = _safe_float(fund.get("price"))
    # -- market cap (₹ Crore everywhere in the UI) ---------------------------
    if fund.get("market_cap") is None and fund.get("mcap_cr") is not None:
        fund["market_cap"] = fund["mcap_cr"]
    if fund.get("mcap_cr") is None and fund.get("market_cap") is not None:
        fund["mcap_cr"] = fund["market_cap"]
    # -- RSI -----------------------------------------------------------------
    if fund.get("rsi14") is None and fund.get("rsi") is not None:
        fund["rsi14"] = fund["rsi"]
    if fund.get("rsi") is None and fund.get("rsi14") is not None:
        fund["rsi"] = fund["rsi14"]
    # -- MACD bull flag -------------------------------------------------------
    if "macd_bull" not in fund or fund.get("macd_bull") is None:
        hist = _safe_float(fund.get("macd_hist"))
        line = _safe_float(fund.get("macd_line"))
        sig = _safe_float(fund.get("macd_signal"))
        bull: bool | None = None
        if hist is not None:
            bull = hist > 0
        elif line is not None and sig is not None:
            bull = line > sig
        fund["macd_bull"] = bool(bull) if bull is not None else False
    # -- above 200-day average -------------------------------------------------
    if "above_ema200" not in fund or fund.get("above_ema200") is None:
        sma200 = _safe_float(fund.get("sma_200"))
        ema200 = _safe_float(fund.get("ema_200"))
        ref = sma200 if sma200 is not None else ema200
        fund["above_ema200"] = bool(price is not None and ref is not None and price >= ref)
    fund.setdefault("above_sma200", fund.get("above_ema200"))
    # -- 52-week / PE / yield back-fill from the merged quote ------------------
    for key in ("wk52_high", "wk52_low", "pe", "div_yield"):
        if fund.get(key) is None and quote.get(key) is not None:
            fund[key] = quote[key]
    if fund.get("div_yield") is None and fund.get("dividend_yield") is not None:
        fund["div_yield"] = fund["dividend_yield"]
    # -- numeric twins for screener-style holding strings ----------------------
    for key in ("promoter_pct", "fii_pct", "dii_pct", "public_pct"):
        num_key = f"{key}_num"
        if fund.get(num_key) is None and fund.get(key) is not None:
            parsed = _parse_pct_string(fund[key])
            if parsed is not None:
                fund[num_key] = parsed
    # -- source badge -----------------------------------------------------------
    sources = set()
    if fund.get("pe") is not None:
        sources.add("yahoo/screener")
    if quote.get("source") and quote.get("source") != "none":
        sources.add(str(quote["source"]))
    if fund.get("promoter_pct") or fund.get("sector_pe"):
        sources.add("screener.in")
    if fund.get("analyst_source"):
        sources.add(str(fund["analyst_source"]))
    fund.setdefault("data_sources", sorted(sources) or ["cache"])
    return fund
