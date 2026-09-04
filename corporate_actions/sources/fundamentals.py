"""Stock fundamentals from Yahoo Finance quoteSummary (crumb-guarded) + chart fallback.

Two public sources, both cached 24h (fundamentals change slowly):
  * Yahoo quoteSummary (needs a cookie + crumb) for price, 52-week high/low,
    P/E, dividend yield, debt/equity and sector.
  * screener.in for sector P/E and promoter/FII/DII holdings (see screener.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from urllib.parse import quote

import requests
try:
    import httpx
except Exception:
    httpx = None
import asyncio

from .. import config
from .analyst_forecast import fill_analyst_fallback
from .http import _quote_session, _throttle_fund_req, _throttle_fund_req_async, _async_client
from .screener import get_competitors, get_sector_pe, parse_screener_fundamentals

log = logging.getLogger(__name__)

_fund_cache: dict = {}
_FUND_CACHE_SECONDS = 86400  # 24 hours
_FUND_RETRY_CACHE_SECONDS = 1800  # 30 min when the screener.in part is still missing
FUND_MAX_ROWS = 40  # rows enriched with the slow screener.in part per command
# Bump this whenever the SHAPE of the cached data changes (new extracted
# fields). Old entries (e.g. fetched before the analyst-forecast extraction
# existed) are then treated as stale and refetched instead of serving a
# forecast-less report for up to 24h.
_FUND_CACHE_SCHEMA = 3

# Disk-persisted session + cache (gitignored .cache/ dir). The always-on bot
# restarts often (Render sleep/wake), and every restart used to throw away
# the Yahoo cookie/crumb and the in-memory fundamentals cache - so the next
# movers screen re-fetched 40+ quoteSummary calls with a fresh consent dance
# and got rate-limited (429), which is why /forecast kept losing the analyst
# section on the deployed bot. Persisting both makes restarts cheap.
_CACHE_DIR = config.BASE_DIR / ".cache"
_SESSION_FILE = _CACHE_DIR / "yahoo_session.json"
_FUND_CACHE_FILE = _CACHE_DIR / "fund_cache.json"

_fund_lock = threading.Lock()
_global_fund_sess = None
_global_fund_crumb = ""
_global_fund_crumb_ts = 0.0
_CRUMB_CACHE_SECONDS = 3600  # 1 hour
_yahoo_lock = threading.Lock()
_yahoo_fail_count = 0
_yahoo_blocked_until = 0.0
_YAHOO_MAX_FAILS = 5
_YAHOO_BLOCK_SECONDS = 300  # pause Yahoo requests for 5 minutes when failing


def _invalidate_crumb():
    """Drop the cached Yahoo crumb so the next call re-fetches it.

    A 401 from quoteSummary means the current crumb is stale (usually after a
    cookie/crumb rotation server-side). The crumb lives in a process-wide global
    shared by every thread, so an expired one must be cleared globally -
    otherwise every /fund would keep 401ing until the 1h TTL expires.
    """
    global _global_fund_crumb, _global_fund_crumb_ts
    _global_fund_crumb = ""
    _global_fund_crumb_ts = 0.0


def _atomic_write(path, text: str) -> None:
    """Write a file atomically (tmp + os.replace) so concurrent readers of
    the persisted cache never see a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _persist_session(sess, crumb: str) -> None:
    """Save the Yahoo cookie jar + crumb so a restart reuses them instead of
    re-doing the consent/crumb dance (a fresh dance after every restart is
    exactly what gets the deployed bot rate-limited)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cookies = requests.utils.dict_from_cookiejar(sess.cookies)
        _atomic_write(_SESSION_FILE, json.dumps({
            "cookies": cookies, "crumb": crumb or "", "saved_at": time.time(),
        }))
    except Exception as error:
        log.info("could not persist yahoo session: %s", error)


def _restore_session(sess) -> str:
    """Reload a previously saved cookie jar + crumb. Returns the crumb or ''."""
    try:
        if _SESSION_FILE.exists():
            payload = json.loads(_SESSION_FILE.read_text())
            cookies = payload.get("cookies") or {}
            if cookies:
                requests.utils.add_dict_to_cookiejar(sess.cookies, cookies)
            return (payload.get("crumb") or "").strip()
    except Exception as error:
        log.info("could not restore yahoo session: %s", error)
    return ""


def _persist_fund_cache() -> None:
    """Best-effort disk copy of the in-memory fundamentals cache.

    Without this, every restart re-fetches every symbol, and the movers
    enrichment (40+ quoteSummary calls per screen) after a fresh consent
    dance is what trips Yahoo's rate limiter - the reason /forecast kept
    losing its analyst section on the deployed bot.
    """
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            [list(key), entry]
            for key, entry in _fund_cache.items()
            if entry.get("data")
        ]
        _atomic_write(_FUND_CACHE_FILE, json.dumps(
            {"schema": _FUND_CACHE_SCHEMA, "entries": entries}, allow_nan=False,
        ))
    except Exception as error:
        log.info("could not persist fund cache: %s", error)


def _load_fund_cache() -> None:
    """Reload the persisted fundamentals cache (same schema only)."""
    try:
        if _FUND_CACHE_FILE.exists():
            payload = json.loads(_FUND_CACHE_FILE.read_text())
            if payload.get("schema") != _FUND_CACHE_SCHEMA:
                return
            loaded = 0
            for key_list, entry in (payload.get("entries") or []):
                if not key_list or not entry:
                    continue
                _fund_cache[tuple(key_list)] = entry
                loaded += 1
            log.info("fund cache restored from disk: %d entries", loaded)
    except Exception as error:
        log.info("could not load fund cache: %s", error)


def _fund_session():
    """A thread-safe global Yahoo session with cached crumb (prevents HTTP 429)."""
    global _global_fund_sess, _global_fund_crumb, _global_fund_crumb_ts
    now = time.time()
    with _fund_lock:
        if _global_fund_sess is None:
            _global_fund_sess = requests.Session()
            _global_fund_sess.headers.update({"User-Agent": config.USER_AGENT})
            restored = _restore_session(_global_fund_sess)
            if restored:
                _global_fund_crumb = restored
                _global_fund_crumb_ts = now
                log.info("fund_session: restored cookie session + crumb from disk")
            else:
                try:
                    response = _global_fund_sess.get("https://fc.yahoo.com", timeout=config.HTTP_TIMEOUT)
                    log.info("fund_session: cookie consent ping -> status %s", response.status_code)
                except Exception as error:
                    log.info("fund_session: cookie consent ping failed: %s", error)

        if not _global_fund_crumb or now - _global_fund_crumb_ts > _CRUMB_CACHE_SECONDS:
            for crumb_host in (
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
            ):
                try:
                    response = _global_fund_sess.get(crumb_host, timeout=config.HTTP_TIMEOUT)
                    if response.status_code == 200 and response.text.strip():
                        _global_fund_crumb = response.text.strip()
                        _global_fund_crumb_ts = now
                        log.info("fund_session: crumb obtained from %s -> %s...", crumb_host, _global_fund_crumb[:6])
                        break
                    log.info("fund_session: crumb from %s -> status %s", crumb_host, response.status_code)
                except Exception as error:
                    log.info("fund_session: crumb request to %s failed: %s", crumb_host, error)
            _persist_session(_global_fund_sess, _global_fund_crumb)

        return _global_fund_sess, _global_fund_crumb


def _quote_summary(symbol: str, suffix: str = ".NS") -> dict | None:
    """Yahoo quoteSummary result (summaryDetail/financialData/defaultKeyStatistics/assetProfile).

    `suffix` picks the exchange: '.NS' for NSE symbols, '' for US tickers.
    """
    now = time.time()
    with _yahoo_lock:
        if now < _yahoo_blocked_until:
            log.info("_quote_summary: Yahoo temporarily blocked until %s", _yahoo_blocked_until)
            return None

    for attempt in range(2):  # second attempt after a 401 crumb refresh
        sess, crumb = _fund_session()
        if not crumb:
            log.info("_quote_summary: no crumb for %s — skipping", symbol)
            return None
        auth_retry = False
        for host in (
            "https://query1.finance.yahoo.com",
            "https://query2.finance.yahoo.com",
        ):
            _throttle_fund_req()
            url = f"{host}/v10/finance/quoteSummary/{quote(symbol)}{suffix}"
            try:
                response = sess.get(
                    url,
                    params={
                        "modules": "summaryDetail,financialData,defaultKeyStatistics,assetProfile,recommendationTrend,upgradesDowngrades",
                        "crumb": crumb,
                    },
                    timeout=config.HTTP_TIMEOUT,
                )
                if response.status_code == 429:
                    log.info("_quote_summary: 429 rate-limited for %s on %s — trying other host", symbol, host)
                    with _yahoo_lock:
                        _yahoo_fail_count += 1
                        if _yahoo_fail_count >= _YAHOO_MAX_FAILS:
                            _yahoo_blocked_until = time.time() + _YAHOO_BLOCK_SECONDS
                            _yahoo_fail_count = 0
                            log.warning("Yahoo appears rate-limited - pausing Yahoo calls for %s seconds", _YAHOO_BLOCK_SECONDS)
                    continue
                if response.status_code == 401:
                    log.info("_quote_summary: 401 for %s on %s — refreshing crumb", symbol, host)
                    _invalidate_crumb()
                    auth_retry = True
                    break
                response.raise_for_status()
                result = response.json()["quoteSummary"]["result"]
                if result:
                    log.info("_quote_summary: OK for %s from %s", symbol, host)
                    return result[0]
                log.info("_quote_summary: empty result for %s from %s", symbol, host)
            except Exception as error:
                log.info("_quote_summary: failed for %s on %s: %s", symbol, host, error)
                with _yahoo_lock:
                    _yahoo_fail_count += 1
                    if _yahoo_fail_count >= _YAHOO_MAX_FAILS:
                        _yahoo_blocked_until = time.time() + _YAHOO_BLOCK_SECONDS
                        _yahoo_fail_count = 0
                        log.warning("Yahoo failing repeatedly - pausing Yahoo calls for %s seconds", _YAHOO_BLOCK_SECONDS)
        if not auth_retry:
            break
    return None


async def _quote_summary_async(symbol: str, suffix: str = ".NS") -> dict | None:
    """Async quoteSummary using httpx.AsyncClient when available.

    Falls back to running the sync `_quote_summary` in a thread when httpx
    is not present or on unexpected failures.
    """
    now = time.time()
    with _yahoo_lock:
        if now < _yahoo_blocked_until:
            log.info("_quote_summary_async: Yahoo temporarily blocked until %s", _yahoo_blocked_until)
            return None
    if httpx is None:
        return await asyncio.to_thread(_quote_summary, symbol, suffix)

    now = time.time()
    if httpx is None:
        return await asyncio.to_thread(_quote_summary, symbol, suffix)

    # Attempt hosts similarly to sync path. Use persisted cookie jar when possible
    # so async requests reuse the same consent state as the sync session.
    persisted_cookies = None
    try:
        if _SESSION_FILE.exists():
            payload = json.loads(_SESSION_FILE.read_text())
            persisted_cookies = payload.get("cookies") or {}
    except Exception:
        persisted_cookies = None

    for attempt in range(2):
        # fetch crumb (best-effort)
        crumb = ""
        for crumb_host in ("https://query1.finance.yahoo.com/v1/test/getcrumb", "https://query2.finance.yahoo.com/v1/test/getcrumb"):
            try:
                async with httpx.AsyncClient(headers={**config.BROWSER_HEADERS}, timeout=config.HTTP_TIMEOUT, cookies=persisted_cookies) as client:
                    r = await client.get(crumb_host)
                    if r.status_code == 200 and (text := r.text.strip()):
                        crumb = text
                        break
            except Exception:
                continue

        for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
            try:
                await _throttle_fund_req_async()
            except Exception:
                pass
            url = f"{host}/v10/finance/quoteSummary/{quote(symbol)}"
            params = {"modules": "summaryDetail,financialData,defaultKeyStatistics,assetProfile,recommendationTrend,upgradesDowngrades", "crumb": crumb}
            try:
                async with httpx.AsyncClient(headers={**config.BROWSER_HEADERS}, timeout=config.HTTP_TIMEOUT, cookies=persisted_cookies) as client:
                    r = await client.get(url, params=params)
                    if r.status_code == 429:
                        with _yahoo_lock:
                            _yahoo_fail_count += 1
                            if _yahoo_fail_count >= _YAHOO_MAX_FAILS:
                                _yahoo_blocked_until = time.time() + _YAHOO_BLOCK_SECONDS
                                _yahoo_fail_count = 0
                                log.warning("Yahoo async appears rate-limited - pausing Yahoo calls for %s seconds", _YAHOO_BLOCK_SECONDS)
                        continue
                    if r.status_code == 401:
                        # crumb stale - try again
                        crumb = ""
                        break
                    r.raise_for_status()
                    result = r.json().get("quoteSummary", {}).get("result")
                    if result:
                        return result[0]
            except Exception:
                with _yahoo_lock:
                    _yahoo_fail_count += 1
                    if _yahoo_fail_count >= _YAHOO_MAX_FAILS:
                        _yahoo_blocked_until = time.time() + _YAHOO_BLOCK_SECONDS
                        _yahoo_fail_count = 0
                        log.warning("Yahoo async failing repeatedly - pausing Yahoo calls for %s seconds", _YAHOO_BLOCK_SECONDS)
                continue
        # if we hit a 401 and broke, retry attempt loop
    return None


def _calculate_rsi(closes: list, period: int = 14) -> float | None:
    """Calculate 14-period Relative Strength Index (RSI) using Wilder's smoothing."""
    prices = [close for close in closes if close is not None]
    if len(prices) < period + 1:
        return None
    # Wilder smoothing implementation
    deltas = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
    gains = [delta if delta > 0 else 0.0 for delta in deltas]
    losses = [-delta if delta < 0 else 0.0 for delta in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period

    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + relative_strength)), 1)
    deltas = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
    gains = [delta if delta > 0 else 0.0 for delta in deltas]
    losses = [-delta if delta < 0 else 0.0 for delta in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period

    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + relative_strength)), 1)


def _ema(values: list, span: int) -> list:
    """Exponential moving average (plain Python, matches pandas ewm adjust=False)."""
    alpha = 2.0 / (span + 1)
    out = []
    previous = None
    for value in values:
        if previous is None:
            previous = value
        else:
            previous = alpha * value + (1.0 - alpha) * previous
        out.append(previous)
    return out


def _calculate_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line / signal line / histogram from daily closes (rounds to 4dp)."""
    prices = [close for close in closes if close is not None]
    if len(prices) < slow + signal:
        return None, None, None
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    line = [fast_value - slow_value for fast_value, slow_value in zip(ema_fast, ema_slow)]
    signal_line = _ema(line, signal)
    return round(line[-1], 4), round(signal_line[-1], 4), round(line[-1] - signal_line[-1], 4)


def _calculate_sma(closes: list, windows: tuple = (50, 200)) -> tuple:
    """Simple moving averages over the trailing windows of closes (None when short)."""
    prices = [close for close in closes if close is not None]
    out = []
    for window in windows:
        if len(prices) < window:
            out.append(None)
        else:
            out.append(round(sum(prices[-window:]) / window, 2))
    return tuple(out)


def _chart_fundamentals(symbol: str, suffix: str = ".NS") -> dict:
    """Extract 52W high/low, PE, dividend yield and 14-day RSI from /v8/finance/chart (no crumb needed).

    `suffix` picks the exchange: '.NS' for NSE symbols, '' for US tickers.
    """
    out = {}
    for host in (
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    ):
        url = (
            f"{host}/v8/finance/chart/{quote(symbol)}{suffix}"
            "?range=1y&interval=1d&includePrePost=false"
        )
        try:
            _throttle_fund_req()
            response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            if response.status_code == 429:
                log.info("_chart_fundamentals: 429 rate-limited for %s on %s — trying other host", symbol, host)
                continue
            response.raise_for_status()
            payload = response.json()
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                log.info("_chart_fundamentals: empty result for %s from %s", symbol, host)
                continue
            meta = result[0].get("meta") or {}
            high = meta.get("fiftyTwoWeekHigh") or meta.get("52WeekHigh")
            low = meta.get("fiftyTwoWeekLow") or meta.get("52WeekLow")
            pe_ratio = meta.get("trailingPE")
            dividend_yield = meta.get("dividendYield")
            if high:
                out["wk52_high"] = high
            if low:
                out["wk52_low"] = low
            if pe_ratio:
                out["pe"] = pe_ratio
            if dividend_yield:
                out["div_yield"] = round(dividend_yield * 100, 2)

            # Technical indicators from daily closes: RSI(14), MACD(12,26,9)
            # and SMA 50/200 - shared by the Indian and US fundamental paths.
            indicators = (result[0].get("indicators") or {}).get("quote") or [{}]
            closes = (indicators[0] or {}).get("close") or []
            if closes:
                rsi_val = _calculate_rsi(closes)
                if rsi_val is not None:
                    out["rsi"] = rsi_val
                macd_line, macd_signal, macd_hist = _calculate_macd(closes)
                if macd_line is not None:
                    out["macd_line"] = macd_line
                if macd_signal is not None:
                    out["macd_signal"] = macd_signal
                if macd_hist is not None:
                    out["macd_hist"] = macd_hist
                sma_50, sma_200 = _calculate_sma(closes)
                if sma_50 is not None:
                    out["sma_50"] = sma_50
                if sma_200 is not None:
                    out["sma_200"] = sma_200

            log.info(
                "_chart_fundamentals: %s -> 52W %.2f-%.2f PE=%s RSI=%s MACD=%s SMA50=%s SMA200=%s from %s",
                symbol, low or 0, high or 0, pe_ratio, out.get("rsi"),
                out.get("macd_line"), out.get("sma_50"), out.get("sma_200"), host,
            )
            break
        except Exception as error:
            log.info("_chart_fundamentals: failed for %s on %s: %s", symbol, host, error)
    return out


async def _chart_fundamentals_async(symbol: str, suffix: str = ".NS") -> dict:
    if httpx is None:
        return await asyncio.to_thread(_chart_fundamentals, symbol, suffix)
    client = _async_client()
    if client is None:
        return await asyncio.to_thread(_chart_fundamentals, symbol, suffix)
    out = {}
    for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        url = f"{host}/v8/finance/chart/{quote(symbol)}{suffix}?range=1y&interval=1d&includePrePost=false"
        try:
            try:
                await _throttle_fund_req_async()
            except Exception:
                pass
            r = await client.get(url, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            payload = r.json()
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                continue
            meta = result[0].get("meta") or {}
            high = meta.get("fiftyTwoWeekHigh") or meta.get("52WeekHigh")
            low = meta.get("fiftyTwoWeekLow") or meta.get("52WeekLow")
            pe_ratio = meta.get("trailingPE")
            dividend_yield = meta.get("dividendYield")
            if high:
                out["wk52_high"] = high
            if low:
                out["wk52_low"] = low
            if pe_ratio:
                out["pe"] = pe_ratio
            if dividend_yield:
                out["div_yield"] = round(dividend_yield * 100, 2)

            indicators = (result[0].get("indicators") or {}).get("quote") or [{}]
            closes = (indicators[0] or {}).get("close") or []
            if closes:
                rsi_val = _calculate_rsi(closes)
                if rsi_val is not None:
                    out["rsi"] = rsi_val
                macd_line, macd_signal, macd_hist = _calculate_macd(closes)
                if macd_line is not None:
                    out["macd_line"] = macd_line
                if macd_signal is not None:
                    out["macd_signal"] = macd_signal
                if macd_hist is not None:
                    out["macd_hist"] = macd_hist
                sma_50, sma_200 = _calculate_sma(closes)
                if sma_50 is not None:
                    out["sma_50"] = sma_50
                if sma_200 is not None:
                    out["sma_200"] = sma_200
            break
        except Exception:
            continue
    return out


# Deep-fundamental fields that Yahoo quoteSummary should provide. Used to
# judge how COMPLETE a response is: Yahoo occasionally returns only part of
# the modules (e.g. summaryDetail but no financialData), which would render a
# half-empty deep report - so we retry once and shorten the cache TTL.
_DEEP_FUND_KEYS = (
    "mcap_cr", "forward_pe", "price_to_sales", "beta", "price_to_book",
    "book_value", "enterprise_value", "shares_outstanding", "float_shares",
    "trailing_eps", "forward_eps", "target_high", "target_low", "target_mean",
    "target_median", "num_analysts", "total_cash", "cash_per_share",
    "total_debt", "total_revenue", "ebitda", "revenue_per_share",
    "earnings_growth", "revenue_growth", "gross_margin", "ebitda_margin",
    "operating_margin", "profit_margin", "current_ratio", "quick_ratio",
    "free_cashflow", "operating_cashflow",
)


def _deep_completeness(out: dict) -> float:
    """Fraction of the deep-fundamental field set actually present (0..1)."""
    if not out:
        return 0.0
    have = sum(1 for key in _DEEP_FUND_KEYS if out.get(key) is not None)
    return have / len(_DEEP_FUND_KEYS)


def _extract_quote_summary(payload: dict, currency: str = "inr") -> dict:
    """Pull price + deep-fundamentals fields out of a Yahoo quoteSummary result.

    `currency` picks the market-cap unit: 'inr' reports \u20b9 Crore (mcap_cr),
    'usd' reports billions of dollars (mcap_usd) for US tickers.
    """
    summary_detail = payload.get("summaryDetail") or {}
    financial_data = payload.get("financialData") or {}
    default_key_statistics = payload.get("defaultKeyStatistics") or {}
    asset_profile = payload.get("assetProfile") or {}

    def _raw(data, key):
        value = data.get(key) or {}
        return value.get("raw") if isinstance(value, dict) else value

    out = {}
    price = _raw(summary_detail, "regularMarketPrice") or _raw(summary_detail, "currentPrice")
    if price:
        out["price"] = price
    for source_dict, target_key in (
        ("fiftyTwoWeekHigh", "wk52_high"),
        ("fiftyTwoWeekLow", "wk52_low"),
        ("trailingPE", "pe"),
    ):
        value = _raw(summary_detail, source_dict)
        if value:
            out[target_key] = value
    dividend_yield = _raw(summary_detail, "dividendYield")  # fraction (0.0045 -> 0.45%)
    if dividend_yield is not None:
        out["div_yield"] = round(dividend_yield * 100, 2)
    debt_to_equity = _raw(financial_data, "debtToEquity")  # Yahoo reports percent (36.65 -> 0.37)
    if debt_to_equity is not None:
        out["debt_to_equity"] = round(debt_to_equity / 100, 2)
    if asset_profile.get("sector"):
        out["sector"] = asset_profile["sector"]

    extras = {}
    for source_dict, source_key, target_key in (
        (summary_detail, "marketCap", "mcap_cr"),
        (summary_detail, "forwardPE", "forward_pe"),
        (summary_detail, "priceToSalesTrailing12Months", "price_to_sales"),
        (summary_detail, "beta", "beta"),
        (default_key_statistics, "priceToBook", "price_to_book"),
        (default_key_statistics, "bookValue", "book_value"),
        (default_key_statistics, "enterpriseValue", "enterprise_value"),
        (default_key_statistics, "sharesOutstanding", "shares_outstanding"),
        (default_key_statistics, "floatShares", "float_shares"),
        (default_key_statistics, "trailingEps", "trailing_eps"),
        (default_key_statistics, "forwardEps", "forward_eps"),
        (financial_data, "targetHighPrice", "target_high"),
        (financial_data, "targetLowPrice", "target_low"),
        (financial_data, "targetMeanPrice", "target_mean"),
        (financial_data, "targetMedianPrice", "target_median"),
        (financial_data, "numberOfAnalystOpinions", "num_analysts"),
        (financial_data, "totalCash", "total_cash"),
        (financial_data, "totalCashPerShare", "cash_per_share"),
        (financial_data, "totalDebt", "total_debt"),
        (financial_data, "totalRevenue", "total_revenue"),
        (financial_data, "ebitda", "ebitda"),
        (financial_data, "revenuePerShare", "revenue_per_share"),
        (financial_data, "earningsGrowth", "earnings_growth"),
        (financial_data, "revenueGrowth", "revenue_growth"),
        (financial_data, "grossMargins", "gross_margin"),
        (financial_data, "ebitdaMargins", "ebitda_margin"),
        (financial_data, "operatingMargins", "operating_margin"),
        (financial_data, "profitMargins", "profit_margin"),
        (financial_data, "currentRatio", "current_ratio"),
        (financial_data, "quickRatio", "quick_ratio"),
        (financial_data, "freeCashflow", "free_cashflow"),
        (financial_data, "operatingCashflow", "operating_cashflow"),
    ):
        value = _raw(source_dict, source_key)
        if value is not None:
            extras[target_key] = value
    market_cap = extras.pop("mcap_cr", None)
    if market_cap is not None:
        if currency == "usd":
            extras["mcap_usd"] = round(market_cap / 1e9, 2)  # dollars -> $B
        else:
            extras["mcap_cr"] = round(market_cap / 1e7, 1)  # rupees -> Crore
    if asset_profile.get("industry"):
        extras["industry"] = asset_profile["industry"]
    if asset_profile.get("fullTimeEmployees"):
        extras["employees"] = asset_profile["fullTimeEmployees"]
    out.update(extras)

    # Top executives (assetProfile.companyOfficers) - shared by IN + US paths.
    officers = (asset_profile.get("companyOfficers") or [])[:6]
    if officers:

        def _clean(value: str) -> str:
            return re.sub(r"\s+", " ", (value or "").strip())

        out["officers"] = [
            {"name": _clean(officer.get("name")),
             "title": _clean(officer.get("title"))}
            for officer in officers
            if _clean(officer.get("name"))
        ]

    # Analyst rating breakdown (recommendationTrend). The trend array holds
    # one row per period (0m, -1m, -2m, -3m) - we keep the LATEST in
    # rec_trend and the WHOLE history (newest first) in rec_history so no
    # single analyst rating is left out of the forecast.
    _RATING_KEYS = (
        ("strongBuy", "strong_buy"), ("buy", "buy"), ("hold", "hold"),
        ("sell", "sell"), ("strongSell", "strong_sell"),
    )
    trend = ((payload.get("recommendationTrend") or {}).get("trend") or [])
    if trend:
        def _buckets(row: dict) -> dict:
            buckets = {}
            for source_key, target_key in _RATING_KEYS:
                value = row.get(source_key)
                if isinstance(value, int):
                    buckets[target_key] = value
            return buckets

        latest = trend[0] or {}
        rec_trend = _buckets(latest)
        if rec_trend:
            out["rec_trend"] = rec_trend
        history = []
        for row in trend[:4]:
            buckets = _buckets(row or {})
            if buckets:
                history.append({"period": (row or {}).get("period", ""), **buckets})
        if history:
            out["rec_history"] = history
    rec_mean = _raw(financial_data, "recommendationMean")
    if rec_mean is not None:
        out["rec_mean"] = round(float(rec_mean), 2)
    rec_key = (financial_data.get("recommendationKey") or "").strip()
    if rec_key:
        out["rec_key"] = rec_key

    return out


def get_fundamentals(symbol: str, with_screener: bool = True) -> dict | None:
    """Return fundamentals for an NSE symbol, cached.

    Always-attempted Yahoo fields: price, wk52_high, wk52_low, pe,
    div_yield (%), debt_to_equity (ratio), sector. When with_screener is
    true (the default) the slow screener.in part (sector_pe, promoter_pct,
    fii_pct, dii_pct) is added as well. Missing keys mean the value could not
    be obtained; None is returned only when nothing at all was available.

    Yahoo occasionally returns a PARTIAL quoteSummary (some modules missing).
    For deep requests we detect that, retry once, and cache partial results
    with a short TTL so the deep report is never served half-empty for long.
    """
    key = symbol.strip().upper()
    # Cache separately per with_screener flag: a fast bulk call (with_screener
    # False) must not poison the deep /fund cache entry for the same symbol,
    # and a deep call must not leak screener.in fields into the fast path.
    cache_key = (key, bool(with_screener))
    now = time.time()
    cached = _fund_cache.get(cache_key)
    if cached and cached.get("schema") == _FUND_CACHE_SCHEMA \
            and now - cached["timestamp"] < cached["time_to_live"]:
        log.info("get_fundamentals: cache hit for %s (screener=%s, %d fields)", key, bool(with_screener), len(cached["data"] or {}))
        return cached["data"]
    if cached and cached.get("schema") != _FUND_CACHE_SCHEMA:
        log.info("get_fundamentals: schema v%s != v%s for %s - refetching", cached.get("schema"), _FUND_CACHE_SCHEMA, key)
    out = {}
    log.info("get_fundamentals: fetching %s (with_screener=%s)", key, with_screener)

    # Primary: Yahoo quoteSummary (needs cookie + crumb)
    payload = _quote_summary(key)
    if payload:
        out = _extract_quote_summary(payload)
        log.info("get_fundamentals: quoteSummary -> %d fields for %s", len(out), key)
    else:
        log.info("get_fundamentals: quoteSummary unavailable for %s — trying chart fallback", key)

    # Yahoo occasionally returns only PART of the modules (e.g. summaryDetail
    # without financialData/defaultKeyStatistics), which leaves the deep
    # report sections empty. Retry once when the deep set looks incomplete.
    if with_screener and _deep_completeness(out) < 0.5:
        log.info(
            "get_fundamentals: %s deep set only %.0f%% complete - retrying quoteSummary once",
            key, _deep_completeness(out) * 100,
        )
        res2 = _quote_summary(key)
        if res2:
            more = _extract_quote_summary(res2)
            out.update({key: value for key, value in more.items() if value is not None})
            log.info(
                "get_fundamentals: retry -> %d fields for %s (was %d)",
                len(out), key, len(out) - len(more),
            )

    # Chart fallback & RSI computation (always run chart to get 14-day RSI and 52W fallback)
    chart_data = _chart_fundamentals(key)
    if chart_data:
        out.update({key: value for key, value in chart_data.items() if key not in out or out[key] is None})
        log.info(
            "get_fundamentals: chart data added for %s: %s",
            key, list(chart_data.keys()),
        )
    else:
        log.info("get_fundamentals: chart data empty for %s", key)

    if with_screener:
        screener_result = parse_screener_fundamentals(key)
        if screener_result:
            out.update({key: value for key, value in screener_result.items() if value is not None})
            log.info("get_fundamentals: screener added %s for %s", list(screener_result.keys()), key)
        else:
            log.info("get_fundamentals: screener empty for %s", key)
        competitors = get_competitors(key)
        if competitors:
            out["competitors"] = competitors
            log.info("get_fundamentals: %d competitors added for %s", len(competitors), key)

    # Independent analyst-forecast fallback when Yahoo's quoteSummary is
    # down/rate-limited: stockanalysis.com (S&P Global + TipRanks) so the
    # forecast section never silently disappears for NSE symbols either.
    fill_analyst_fallback(key, "NSE", out)

    data = out or None
    time_to_live = _FUND_CACHE_SECONDS
    # Shorten the cache when a part is missing so we retry it sooner instead of
    # serving a degraded report for the full 24h. This covers the rate-limited
    # screener.in part AND a partial Yahoo quoteSummary (fewer than half the
    # deep-fundamental fields), so a half-empty deep report never lingers.
    deep_ok = _deep_completeness(out) >= 0.5
    if (with_screener and not (out.get("promoter_pct") or out.get("sector_pe"))) or not deep_ok:
        time_to_live = _FUND_RETRY_CACHE_SECONDS  # retry the missing part sooner
    _fund_cache[cache_key] = {
        "timestamp": now, "data": data, "time_to_live": time_to_live,
        "schema": _FUND_CACHE_SCHEMA,
    }
    _persist_fund_cache()
    log.info(
        "get_fundamentals: done %s -> %d field(s): %s",
        key, len(out), list(out.keys()) if out else [],
    )
    return data


async def get_fundamentals_async(symbol: str, with_screener: bool = True) -> dict | None:
    """Async wrapper around `get_fundamentals`.

    Best-effort helper: prefers async HTTP paths where available, otherwise
    runs sync functions via `asyncio.to_thread` so callers can `await` it.
    """
    key = symbol.strip().upper()
    out = {}
    # Primary: try async quoteSummary when httpx present
    if httpx is not None:
        payload = await _quote_summary_async(key)
    else:
        payload = await asyncio.to_thread(_quote_summary, key)
    if payload:
        out = _extract_quote_summary(payload)

    # Retry logic if incomplete
    if with_screener and _deep_completeness(out) < 0.5:
        if httpx is not None:
            res2 = await _quote_summary_async(key)
        else:
            res2 = await asyncio.to_thread(_quote_summary, key)
        if res2:
            more = _extract_quote_summary(res2)
            out.update({k: v for k, v in more.items() if v is not None})

    # Chart fallback
    if httpx is not None:
        chart_data = await _chart_fundamentals_async(key)
    else:
        chart_data = await asyncio.to_thread(_chart_fundamentals, key)
    if chart_data:
        out.update({k: v for k, v in chart_data.items() if k not in out or out[k] is None})

    # Screener + competitors (prefer async helpers)
    if with_screener:
        try:
            from .screener import parse_screener_fundamentals_async, get_competitors_async
            screener_result = await parse_screener_fundamentals_async(key)
        except Exception:
            screener_result = await asyncio.to_thread(parse_screener_fundamentals, key)
        if screener_result:
            out.update({k: v for k, v in screener_result.items() if v is not None})
        try:
            competitors = await get_competitors_async(key)
        except Exception:
            competitors = await asyncio.to_thread(get_competitors, key)
        if competitors:
            out["competitors"] = competitors

    # Analyst fallback (prefer async helper when available)
    try:
        from .analyst_forecast import fill_analyst_fallback_async
        await fill_analyst_fallback_async(key, "NSE", out)
    except Exception:
        await asyncio.to_thread(fill_analyst_fallback, key, "NSE", out)

    data = out or None
    now = time.time()
    time_to_live = _FUND_CACHE_SECONDS
    deep_ok = _deep_completeness(out) >= 0.5
    if (with_screener and not (out.get("promoter_pct") or out.get("sector_pe"))) or not deep_ok:
        time_to_live = _FUND_RETRY_CACHE_SECONDS
    _fund_cache[(key, bool(with_screener))] = {
        "timestamp": now, "data": data, "time_to_live": time_to_live,
        "schema": _FUND_CACHE_SCHEMA,
    }
    _persist_fund_cache()
    return data


# Restore the disk-persisted fundamentals cache at import time so a restart
# reuses cached Yahoo data instead of re-fetching everything (which is what
# trips Yahoo's rate limiter on the always-on bot).
_load_fund_cache()
