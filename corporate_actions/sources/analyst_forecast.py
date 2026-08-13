"""Analyst-forecast fallback sources, used when Yahoo's quoteSummary is down.

Yahoo quoteSummary is the primary analyst source (it feeds rec_mean,
rec_trend, num_analysts and the price targets). When it is unavailable or
rate-limited, this module tries an independent source:

  * stockanalysis.com (US tickers) - consensus rating, analyst count and
    price-target high/low/average/median from S&P Global Market
    Intelligence plus a monthly buy/hold/sell rating breakdown from
    TipRanks. Server-rendered, no auth, no crumb.

Indian NSE/BSE symbols have no free scrapeable analyst-consensus source
besides Yahoo (screener.in and MoneyControl consensus are paid features,
TradingView's India scanner carries no analyst fields, ET's pages are
templated), so the Indian resilience strategy is to keep Yahoo working -
see the persisted session + cache in fundamentals.py.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from .. import config

log = logging.getLogger(__name__)

_stockanalysis_cache: dict = {}
_STOCKANALYSIS_CACHE_SECONDS = 86400  # 24h - analyst data changes slowly


def _parse_summary(html: str) -> dict | None:
    """Parse the summary sentence: analyst count, consensus, average target.

    e.g. "According to 46 analysts polled by S&P Global, Apple stock has a
    consensus rating of \"Buy\" and an average price target of $322.28."
    """
    match = re.search(
        r"According to (\d+) analysts[\s\S]{0,220}?"
        r"consensus rating of [\"'](\w+)[\"'][\s\S]{0,140}?"
        r"average price target of \$([\d,]+(?:\.\d+)?)",
        html, re.I,
    )
    if not match:
        return None
    return {
        "num_analysts": int(match.group(1)),
        "rec_key": match.group(2).lower(),
        "target_mean": float(match.group(3).replace(",", "")),
    }


def _parse_price_targets(html: str) -> dict | None:
    """Parse the price-target row (Low / Average / Median / High)."""
    match = re.search(
        r"<th[^>]*>\s*Low\s*</th>.*?<th[^>]*>\s*Average\s*</th>.*?"
        r"<th[^>]*>\s*Median\s*</th>.*?<th[^>]*>\s*High\s*</th>.*?"
        r"<td[^>]*>\s*Price\s*</td>(.*?)</tr>",
        html, re.S | re.I,
    )
    if not match:
        return None
    values = [float(value) for value in re.findall(r"\$([\d,]+(?:\.\d+)?)", match.group(1))]
    if len(values) < 4:
        return None
    return {
        "target_low": values[0],
        "target_mean": values[1],
        "target_median": values[2],
        "target_high": values[3],
    }


def _parse_ratings(html: str) -> dict | None:
    """Parse the latest monthly rating breakdown from the embedded JSON.

    The page embeds recommendations:[{buy:5,hold:15,sell:3,strongBuy:23,
    strongSell:1,total:48,consensus:"Buy",...}...] - unquoted keys, so it is
    matched with regex and the LAST (current-month) entry is used.
    """
    # The embedded JSON has unquoted keys and the fields appear in varying
    # order ({buy:5,...,total:48,...,consensus:"Buy",strongBuy:23,...}), so
    # each {..} segment is extracted and the numeric fields are pulled out
    # individually. The LAST entry is the current month.
    entries = []
    for segment in re.findall(r"\{[^{}]*\}", html):
        if "strongBuy" not in segment or "total" not in segment:
            continue

        def _num(key: str) -> int:
            match = re.search(r"\b" + key + r":(\d+)", segment)
            return int(match.group(1)) if match else 0

        trend = {
            "strong_buy": _num("strongBuy"),
            "buy": _num("buy"),
            "hold": _num("hold"),
            "sell": _num("sell"),
            "strong_sell": _num("strongSell"),
        }
        if sum(trend.values()) > 0:
            entries.append((trend, _num("total")))
    if not entries:
        return None
    trend, total = entries[-1]
    return {
        "rec_trend": trend,
        "num_analysts": total or sum(trend.values()),
    }


def get_stockanalysis_forecast(symbol: str) -> dict | None:
    """US analyst forecast from stockanalysis.com (S&P Global + TipRanks).

    Returns the fund-style analyst keys (num_analysts, rec_key, rec_trend,
    target_high/low/mean/median) plus analyst_source, or None.
    """
    ticker = (symbol or "").strip().upper()
    if not ticker:
        return None
    now = time.time()
    cached = _stockanalysis_cache.get(ticker)
    if cached and now - cached["timestamp"] < _STOCKANALYSIS_CACHE_SECONDS:
        log.debug("stockanalysis cache hit for %s", ticker)
        return cached["data"]
    data = None
    try:
        response = requests.get(
            f"https://stockanalysis.com/stocks/{ticker.lower()}/forecast/",
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            log.info("stockanalysis: %s -> status %s", ticker, response.status_code)
        else:
            html = response.text
            parsed = {}
            for part in (_parse_summary(html), _parse_price_targets(html), _parse_ratings(html)):
                if part:
                    parsed.update(part)
            if parsed:
                parsed["analyst_source"] = "stockanalysis.com (S&P Global + TipRanks)"
                data = parsed
                log.info("stockanalysis: parsed forecast for %s: %s", ticker, list(parsed))
    except Exception as error:
        log.info("stockanalysis: failed for %s: %s", ticker, error)
    _stockanalysis_cache[ticker] = {"timestamp": now, "data": data}
    return data


def fill_analyst_fallback(symbol: str, exchange: str, out: dict) -> bool:
    """Merge an independent analyst-forecast source into `out`.

    Only runs when the Yahoo analyst keys are missing entirely (the forecast
    section would otherwise fall back to the "unavailable" note). Returns
    True when an independent source was merged in.
    """
    has_analyst = any(out.get(key) for key in
                      ("rec_mean", "rec_trend", "num_analysts", "target_mean"))
    if has_analyst:
        return False
    if (exchange or "").upper() in ("US", "NASDAQ", "NYSE"):
        fallback = get_stockanalysis_forecast(symbol)
        if fallback:
            for key in ("rec_trend", "num_analysts", "rec_key",
                        "target_mean", "target_high", "target_low",
                        "target_median", "analyst_source"):
                if fallback.get(key) is not None:
                    out[key] = fallback[key]
            log.info("analyst fallback (stockanalysis.com) merged for %s", symbol)
            return True
    return False
