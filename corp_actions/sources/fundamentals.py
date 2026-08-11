"""Stock fundamentals from Yahoo Finance quoteSummary (crumb-guarded) + chart fallback.

Two public sources, both cached 24h (fundamentals change slowly):
  * Yahoo quoteSummary (needs a cookie + crumb) for price, 52-week high/low,
    P/E, dividend yield, debt/equity and sector.
  * screener.in for sector P/E and promoter/FII/DII holdings (see screener.py).
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote

import requests

from .. import config
from .http import _quote_session, _throttle_fund_req
from .screener import get_sector_pe, parse_screener_fundamentals

log = logging.getLogger(__name__)

_fund_cache: dict = {}
_FUND_TTL = 86400  # 24 hours
_FUND_RETRY_TTL = 1800  # 30 min when the screener.in part is still missing
FUND_MAX_ROWS = 40  # rows enriched with the slow screener.in part per command

_fund_lock = threading.Lock()
_global_fund_sess = None
_global_fund_crumb = ""
_global_fund_crumb_ts = 0.0
_CRUMB_TTL = 3600  # 1 hour


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


def _fund_session():
    """A thread-safe global Yahoo session with cached crumb (prevents HTTP 429)."""
    global _global_fund_sess, _global_fund_crumb, _global_fund_crumb_ts
    now = time.time()
    with _fund_lock:
        if _global_fund_sess is None:
            _global_fund_sess = requests.Session()
            _global_fund_sess.headers.update({"User-Agent": config.USER_AGENT})
            try:
                r = _global_fund_sess.get("https://fc.yahoo.com", timeout=config.HTTP_TIMEOUT)
                log.info("fund_session: cookie consent ping -> status %s", r.status_code)
            except Exception as exc:
                log.info("fund_session: cookie consent ping failed: %s", exc)

        if not _global_fund_crumb or now - _global_fund_crumb_ts > _CRUMB_TTL:
            for crumb_host in (
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
            ):
                try:
                    resp = _global_fund_sess.get(crumb_host, timeout=config.HTTP_TIMEOUT)
                    if resp.status_code == 200 and resp.text.strip():
                        _global_fund_crumb = resp.text.strip()
                        _global_fund_crumb_ts = now
                        log.info("fund_session: crumb obtained from %s -> %s...", crumb_host, _global_fund_crumb[:6])
                        break
                    log.info("fund_session: crumb from %s -> status %s", crumb_host, resp.status_code)
                except Exception as exc:
                    log.info("fund_session: crumb request to %s failed: %s", crumb_host, exc)

        return _global_fund_sess, _global_fund_crumb


def _quote_summary(symbol: str) -> dict | None:
    """Yahoo quoteSummary result (summaryDetail/financialData/defaultKeyStatistics/assetProfile)."""
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
            url = f"{host}/v10/finance/quoteSummary/{quote(symbol)}.NS"
            try:
                resp = sess.get(
                    url,
                    params={
                        "modules": "summaryDetail,financialData,defaultKeyStatistics,assetProfile",
                        "crumb": crumb,
                    },
                    timeout=config.HTTP_TIMEOUT,
                )
                if resp.status_code == 429:
                    log.info("_quote_summary: 429 rate-limited for %s on %s — trying other host", symbol, host)
                    continue
                if resp.status_code == 401:
                    log.info("_quote_summary: 401 for %s on %s — refreshing crumb", symbol, host)
                    _invalidate_crumb()
                    auth_retry = True
                    break
                resp.raise_for_status()
                result = resp.json()["quoteSummary"]["result"]
                if result:
                    log.info("_quote_summary: OK for %s from %s", symbol, host)
                    return result[0]
                log.info("_quote_summary: empty result for %s from %s", symbol, host)
            except Exception as exc:
                log.info("_quote_summary: failed for %s on %s: %s", symbol, host, exc)
        if not auth_retry:
            break
    return None


def _calculate_rsi(closes: list, period: int = 14) -> float | None:
    """Calculate 14-period Relative Strength Index (RSI) using Wilder's smoothing."""
    prices = [c for c in closes if c is not None]
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _chart_fundamentals(symbol: str) -> dict:
    """Extract 52W high/low, PE, dividend yield and 14-day RSI from /v8/finance/chart (no crumb needed)."""
    out = {}
    for host in (
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    ):
        url = (
            f"{host}/v8/finance/chart/{quote(symbol)}.NS"
            "?range=1y&interval=1d&includePrePost=false"
        )
        try:
            _throttle_fund_req()
            resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 429:
                log.info("_chart_fundamentals: 429 rate-limited for %s on %s — trying other host", symbol, host)
                continue
            resp.raise_for_status()
            res = resp.json()
            result = (res.get("chart") or {}).get("result") or []
            if not result:
                log.info("_chart_fundamentals: empty result for %s from %s", symbol, host)
                continue
            meta = result[0].get("meta") or {}
            hi = meta.get("fiftyTwoWeekHigh") or meta.get("52WeekHigh")
            lo = meta.get("fiftyTwoWeekLow") or meta.get("52WeekLow")
            pe = meta.get("trailingPE")
            dy = meta.get("dividendYield")
            if hi:
                out["wk52_high"] = hi
            if lo:
                out["wk52_low"] = lo
            if pe:
                out["pe"] = pe
            if dy:
                out["div_yield"] = round(dy * 100, 2)

            # Calculate 14-day RSI from daily closing prices
            indicators = (result[0].get("indicators") or {}).get("quote") or [{}]
            closes = (indicators[0] or {}).get("close") or []
            if closes:
                rsi_val = _calculate_rsi(closes)
                if rsi_val is not None:
                    out["rsi"] = rsi_val

            log.info(
                "_chart_fundamentals: %s -> 52W %.2f-%.2f PE=%s RSI=%s from %s",
                symbol, lo or 0, hi or 0, pe, out.get("rsi"), host,
            )
            break
        except Exception as exc:
            log.info("_chart_fundamentals: failed for %s on %s: %s", symbol, host, exc)
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
    have = sum(1 for k in _DEEP_FUND_KEYS if out.get(k) is not None)
    return have / len(_DEEP_FUND_KEYS)


def _extract_quote_summary(res: dict) -> dict:
    """Pull price + deep-fundamentals fields out of a Yahoo quoteSummary result."""
    sd = res.get("summaryDetail") or {}
    fd = res.get("financialData") or {}
    dks = res.get("defaultKeyStatistics") or {}
    ap = res.get("assetProfile") or {}

    def _raw(d, k):
        v = d.get(k) or {}
        return v.get("raw") if isinstance(v, dict) else v

    out = {}
    price = _raw(sd, "regularMarketPrice") or _raw(sd, "currentPrice")
    if price:
        out["price"] = price
    for src, dst in (
        ("fiftyTwoWeekHigh", "wk52_high"),
        ("fiftyTwoWeekLow", "wk52_low"),
        ("trailingPE", "pe"),
    ):
        val = _raw(sd, src)
        if val:
            out[dst] = val
    dy = _raw(sd, "dividendYield")  # fraction (0.0045 -> 0.45%)
    if dy is not None:
        out["div_yield"] = round(dy * 100, 2)
    de = _raw(fd, "debtToEquity")  # Yahoo reports percent (36.65 -> 0.37)
    if de is not None:
        out["debt_to_equity"] = round(de / 100, 2)
    if ap.get("sector"):
        out["sector"] = ap["sector"]

    extras = {}
    for src, src_key, dst_key in (
        (sd, "marketCap", "mcap_cr"),
        (sd, "forwardPE", "forward_pe"),
        (sd, "priceToSalesTrailing12Months", "price_to_sales"),
        (sd, "beta", "beta"),
        (dks, "priceToBook", "price_to_book"),
        (dks, "bookValue", "book_value"),
        (dks, "enterpriseValue", "enterprise_value"),
        (dks, "sharesOutstanding", "shares_outstanding"),
        (dks, "floatShares", "float_shares"),
        (dks, "trailingEps", "trailing_eps"),
        (dks, "forwardEps", "forward_eps"),
        (fd, "targetHighPrice", "target_high"),
        (fd, "targetLowPrice", "target_low"),
        (fd, "targetMeanPrice", "target_mean"),
        (fd, "targetMedianPrice", "target_median"),
        (fd, "numberOfAnalystOpinions", "num_analysts"),
        (fd, "totalCash", "total_cash"),
        (fd, "totalCashPerShare", "cash_per_share"),
        (fd, "totalDebt", "total_debt"),
        (fd, "totalRevenue", "total_revenue"),
        (fd, "ebitda", "ebitda"),
        (fd, "revenuePerShare", "revenue_per_share"),
        (fd, "earningsGrowth", "earnings_growth"),
        (fd, "revenueGrowth", "revenue_growth"),
        (fd, "grossMargins", "gross_margin"),
        (fd, "ebitdaMargins", "ebitda_margin"),
        (fd, "operatingMargins", "operating_margin"),
        (fd, "profitMargins", "profit_margin"),
        (fd, "currentRatio", "current_ratio"),
        (fd, "quickRatio", "quick_ratio"),
        (fd, "freeCashflow", "free_cashflow"),
        (fd, "operatingCashflow", "operating_cashflow"),
    ):
        val = _raw(src, src_key)
        if val is not None:
            extras[dst_key] = val
    mc = extras.get("mcap_cr")
    if mc is not None:
        extras["mcap_cr"] = round(mc / 1e7, 1)  # rupees -> Crore
    if ap.get("industry"):
        extras["industry"] = ap["industry"]
    if ap.get("fullTimeEmployees"):
        extras["employees"] = ap["fullTimeEmployees"]
    out.update(extras)
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
    if cached and now - cached["ts"] < cached["ttl"]:
        log.info("get_fundamentals: cache hit for %s (screener=%s, %d fields)", key, bool(with_screener), len(cached["data"] or {}))
        return cached["data"]
    out = {}
    log.info("get_fundamentals: fetching %s (with_screener=%s)", key, with_screener)

    # Primary: Yahoo quoteSummary (needs cookie + crumb)
    res = _quote_summary(key)
    if res:
        out = _extract_quote_summary(res)
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
            out.update({k: v for k, v in more.items() if v is not None})
            log.info(
                "get_fundamentals: retry -> %d fields for %s (was %d)",
                len(out), key, len(out) - len(more),
            )

    # Chart fallback & RSI computation (always run chart to get 14-day RSI and 52W fallback)
    chart_data = _chart_fundamentals(key)
    if chart_data:
        out.update({k: v for k, v in chart_data.items() if k not in out or out[k] is None})
        log.info(
            "get_fundamentals: chart data added for %s: %s",
            key, list(chart_data.keys()),
        )
    else:
        log.info("get_fundamentals: chart data empty for %s", key)

    if with_screener:
        scr = parse_screener_fundamentals(key)
        if scr:
            out.update({k: v for k, v in scr.items() if v is not None})
            log.info("get_fundamentals: screener added %s for %s", list(scr.keys()), key)
        else:
            log.info("get_fundamentals: screener empty for %s", key)

    data = out or None
    ttl = _FUND_TTL
    # Shorten the cache when a part is missing so we retry it sooner instead of
    # serving a degraded report for the full 24h. This covers the rate-limited
    # screener.in part AND a partial Yahoo quoteSummary (fewer than half the
    # deep-fundamental fields), so a half-empty deep report never lingers.
    deep_ok = _deep_completeness(out) >= 0.5
    if (with_screener and not (out.get("promoter_pct") or out.get("sector_pe"))) or not deep_ok:
        ttl = _FUND_RETRY_TTL  # retry the missing part sooner
    _fund_cache[cache_key] = {"ts": now, "data": data, "ttl": ttl}
    log.info(
        "get_fundamentals: done %s -> %d field(s): %s",
        key, len(out), list(out.keys()) if out else [],
    )
    return data
