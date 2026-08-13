"""Analyst-forecast fallback sources, used when Yahoo's quoteSummary is down.

Yahoo quoteSummary is the primary analyst source (it feeds rec_mean,
rec_trend, num_analysts and the price targets). When it is unavailable or
rate-limited, this module tries an independent source:

  * stockanalysis.com - consensus rating, analyst count and price-target
    high/low/average/median from S&P Global Market Intelligence plus a
    monthly buy/hold/sell rating breakdown from TipRanks. Server-rendered,
    no auth, no crumb. Covers US tickers (/stocks/...) AND Indian NSE
    symbols (/quote/nse/...); stockanalysis.com has no BSE pages.
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

_BASE_URL = "https://stockanalysis.com"


def _stockanalysis_url(ticker: str, exchange: str) -> str:
    """Forecast-page URL for a ticker on the given exchange."""
    exchange = (exchange or "").upper()
    if exchange in ("US", "NASDAQ", "NYSE"):
        return f"{_BASE_URL}/stocks/{ticker.lower()}/forecast/"
    return f"{_BASE_URL}/quote/nse/{ticker.lower()}/forecast/"


def _parse_summary(html: str) -> dict | None:
    """Parse the summary sentence: analyst count, consensus, average target.

    e.g. US: "According to 46 analysts polled by S&P Global, Apple stock has
    a consensus rating of "Buy" and an average price target of $322.28."
    NSE: "According to 34 analysts polled by S&P Global, Godrej Consumer
    Products stock has a consensus rating of "Buy" and an average price
    target of ₹1,240."
    """
    match = re.search(
        r"According to (\d+) analysts[\s\S]{0,220}?"
        r"consensus rating of [\"'](\w+)[\"'][\s\S]{0,140}?"
        r"average price target of [^\d]{0,6}([\d,]+(?:\.\d+)?)",
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
    """Parse the price-target row (Low / Average / Median / High).

    US layout:  <th>Low</th><th>Average</th><th>Median</th><th>High</th>
                <td>Price</td><td>$322.28</td><td>...</td>
    NSE layout: <th>Target</th><th>Low</th><th>Average</th><th>Median</th>
                <th>High</th><td>Price</td><td>₹772.00</td><td>...</td>
    The optional "Target" header column and any currency symbol are handled,
    so the same parser works for both layouts.
    """
    match = re.search(
        r"<th[^>]*>\s*(?:Target\s*</th>\s*<th[^>]*>)?Low\s*</th>.*?"
        r"<th[^>]*>\s*Average\s*</th>.*?<th[^>]*>\s*Median\s*</th>.*?"
        r"<th[^>]*>\s*High\s*</th>.*?<td[^>]*>\s*Price\s*</td>(.*?)</tr>",
        html, re.S | re.I,
    )
    if not match:
        return None
    values = [float(value.replace(",", ""))
              for value in re.findall(r"([\d,]+(?:\.\d+)?)", match.group(1))]
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


def get_stockanalysis_forecast(symbol: str, exchange: str = "US") -> dict | None:
    """Analyst forecast from stockanalysis.com (S&P Global + TipRanks).

    Returns the fund-style analyst keys (num_analysts, rec_key, rec_trend,
    target_high/low/mean/median) plus analyst_source, or None. `exchange`
    picks the page: 'US'/'NASDAQ'/'NYSE' -> /stocks/, 'BSE' -> /quote/bse/,
    anything else -> /quote/nse/ (Indian).
    """
    ticker = (symbol or "").strip().upper()
    exchange = (exchange or "").upper()
    if not ticker:
        return None
    now = time.time()
    cache_key = (exchange, ticker)
    cached = _stockanalysis_cache.get(cache_key)
    if cached and now - cached["timestamp"] < _STOCKANALYSIS_CACHE_SECONDS:
        log.debug("stockanalysis cache hit for %s", cache_key)
        return cached["data"]
    data = None
    try:
        response = requests.get(
            _stockanalysis_url(ticker, exchange),
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            log.info("stockanalysis: %s -> status %s", cache_key, response.status_code)
        else:
            html = response.text
            summary = _parse_summary(html)
            targets = _parse_price_targets(html)
            ratings = _parse_ratings(html)
            parsed = {}
            for part in (summary, targets, ratings):
                if not part:
                    continue
                if part is ratings and summary and summary.get("num_analysts"):
                    # The S&P polled analyst count (summary) wins over the
                    # monthly TipRanks total when both are present.
                    part = {key: value for key, value in part.items()
                            if key != "num_analysts"}
                parsed.update(part)
            if parsed:
                parsed["analyst_source"] = "stockanalysis.com (S&P Global + TipRanks)"
                data = parsed
                log.info("stockanalysis: parsed forecast for %s: %s",
                         cache_key, list(parsed))
    except Exception as error:
        log.info("stockanalysis: failed for %s: %s", cache_key, error)
    _stockanalysis_cache[cache_key] = {"timestamp": now, "data": data}
    return data


def fill_analyst_fallback(symbol: str, exchange: str, out: dict) -> bool:
    """Merge an independent analyst-forecast source into `out`.

    Only runs when the Yahoo analyst keys are missing entirely (the forecast
    section would otherwise fall back to the "unavailable" note). Returns
    True when an independent source was merged in. Works for US tickers and
    Indian NSE/BSE symbols (stockanalysis.com covers both).
    """
    has_analyst = any(out.get(key) for key in
                      ("rec_mean", "rec_trend", "num_analysts", "target_mean"))
    if has_analyst:
        return False
    if (exchange or "").upper() in ("US", "NASDAQ", "NYSE", "NSE", "BSE"):
        fallback = get_stockanalysis_forecast(symbol, exchange)
        if fallback:
            for key in ("rec_trend", "num_analysts", "rec_key",
                        "target_mean", "target_high", "target_low",
                        "target_median", "analyst_source"):
                if fallback.get(key) is not None:
                    out[key] = fallback[key]
            log.info("analyst fallback (stockanalysis.com) merged for %s:%s",
                     exchange, symbol)
            return True
    return False
