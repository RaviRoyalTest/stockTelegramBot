"""US-ticker fundamentals (Yahoo Finance, no exchange suffix, USD units).

Shares the Yahoo client plumbing with the Indian path (fundamentals.py) -
the cookie/crumb session, the quoteSummary + chart extractors and the shared
cache - but never touches India-only sources (no .NS suffix, no screener.in).
Market cap comes back as mcap_usd ($B); ROE/ROCE are normalised from Yahoo's
fractions to percents so the US report renders them like the Indian one.
"""
from __future__ import annotations

import logging
import time

from .analyst_forecast import fill_analyst_fallback
from .fundamentals import (
    _FUND_CACHE_SECONDS,
    _chart_fundamentals,
    _extract_quote_summary,
    _fund_cache,
    _persist_fund_cache,
    _quote_summary,
)

log = logging.getLogger(__name__)


def get_us_fundamentals(symbol: str) -> dict | None:
    """Return fundamentals for a US ticker (no exchange suffix, USD), cached.

    Uses the same Yahoo quoteSummary + chart sources as the Indian path but
    without the '.NS' suffix, and skips the screener.in part (India-only).
    Market cap comes back as mcap_usd ($B); every other field matches the
    Indian naming (pe, forward_pe, debt_to_equity, div_yield, margins, ...).
    Returns None only when nothing at all was available.
    """
    key = symbol.strip().upper()
    cache_key = ("us", key)
    now = time.time()
    cached = _fund_cache.get(cache_key)
    if cached and now - cached["timestamp"] < cached["time_to_live"]:
        log.info("get_us_fundamentals: cache hit for %s (%d fields)", key, len(cached["data"] or {}))
        return cached["data"]
    out = {}
    log.info("get_us_fundamentals: fetching %s", key)
    payload = _quote_summary(key, suffix="")
    if payload:
        out = _extract_quote_summary(payload, currency="usd")
        # Yahoo reports ROE/ROCE as fractions (0.15 = 15%) while the Indian
        # screener.in values are plain percents - normalise to percents here
        # so the US report can render them like the Indian one (% sign).
        financial_data = payload.get("financialData") or {}
        for source_key, target_key in (
            ("returnOnEquity", "roe"),
            ("returnOnCapitalEmployed", "roce"),
        ):
            raw = financial_data.get(source_key) or {}
            value = raw.get("raw") if isinstance(raw, dict) else raw
            if value is not None:
                out[target_key] = round(float(value) * 100, 1)
        raw = financial_data.get("pegRatio") or {}
        peg = raw.get("raw") if isinstance(raw, dict) else raw
        if peg is not None:
            out["peg"] = round(float(peg), 2)
        log.info("get_us_fundamentals: quoteSummary -> %d fields for %s", len(out), key)
    else:
        log.info("get_us_fundamentals: quoteSummary unavailable for %s — trying chart fallback", key)
    chart_data = _chart_fundamentals(key, suffix="")
    if chart_data:
        out.update({k: value for k, value in chart_data.items() if k not in out or out[k] is None})
        log.info("get_us_fundamentals: chart data added for %s: %s", key, list(chart_data.keys()))
    # Independent analyst-forecast fallback when Yahoo's quoteSummary is
    # down/rate-limited: stockanalysis.com (S&P Global + TipRanks) so the
    # forecast section never silently disappears for US tickers.
    fill_analyst_fallback(key, "US", out)
    data = out or None
    _fund_cache[cache_key] = {
        "timestamp": now, "data": data, "time_to_live": _FUND_CACHE_SECONDS,
    }
    _persist_fund_cache()
    log.info("get_us_fundamentals: done %s -> %d field(s)", key, len(out))
    return data
