"""Free (no-key) precious-metal + FX quotes for the Commodities tools.

Mirrors the React app's gold-silver hook (three public sources, consensus
of the closest pair, 5-minute cache):

  * CoinPaprika - PAX Gold + Kinesis Silver tickers (USD/oz).
  * CoinGecko   - pax-gold + kinesis-silver simple prices (USD/oz).
  * currency-api - USD/XAU + USD/XAG rates (price = 1/rate) plus USD/INR
    for the per-gram INR figures.

No API key anywhere. Every source degrades independently; the consensus
price is the average of the closest pair (or the plain average of two, or
the single value), exactly like the reference implementation.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from .. import config
from .http import _quote_session

log = logging.getLogger(__name__)

_METALS_CACHE: dict = {}
_METALS_TTL = 300  # seconds - same 5-minute cache as the reference app


def _safe_float(value) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if out != out or out in (float("inf"), float("-inf")) or out <= 0:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _coinpaprika() -> dict | None:
    """PAX Gold + Kinesis Silver USD prices from CoinPaprika tickers."""
    try:
        sess = _quote_session()
        gold = sess.get("https://api.coinpaprika.com/v1/tickers/paxg-pax-gold",
                        timeout=config.HTTP_TIMEOUT)
        silver = sess.get("https://api.coinpaprika.com/v1/tickers/kag-kinesis-silver",
                          timeout=config.HTTP_TIMEOUT)
        if not gold.ok or not silver.ok:
            return None
        g_price = _safe_float((gold.json().get("quotes") or {}).get("USD", {}).get("price"))
        s_price = _safe_float((silver.json().get("quotes") or {}).get("USD", {}).get("price"))
        if g_price is None or s_price is None:
            return None
        return {"gold": g_price, "silver": s_price, "source": "CoinPaprika",
                "time": datetime.now().strftime("%H:%M:%S")}
    except Exception as error:
        log.info("metals: CoinPaprika failed: %s", error)
        return None


def _coingecko() -> dict | None:
    """PAX Gold + Kinesis Silver USD prices from CoinGecko simple prices."""
    try:
        resp = _quote_session().get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=pax-gold,kinesis-silver&vs_currencies=usd&include_last_updated_at=true",
            timeout=config.HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if not resp.ok:
            return None
        payload = resp.json() or {}
        g_price = _safe_float((payload.get("pax-gold") or {}).get("usd"))
        s_price = _safe_float((payload.get("kinesis-silver") or {}).get("usd"))
        if g_price is None or s_price is None:
            return None
        return {"gold": g_price, "silver": s_price, "source": "CoinGecko", "time": ""}
    except Exception as error:
        log.info("metals: CoinGecko failed: %s", error)
        return None


def _currency_api() -> dict | None:
    """Gold/silver USD prices + USD/INR from the free currency-api dataset.

    data.usd.xau is "ounces of gold per 1 USD", so price = 1/rate.
    """
    try:
        resp = _quote_session().get(
            "https://latest.currency-api.pages.dev/v1/currencies/usd.json",
            timeout=config.HTTP_TIMEOUT,
        )
        if not resp.ok:
            return None
        payload = resp.json() or {}
        usd = payload.get("usd") or {}
        xau = _safe_float(usd.get("xau"))
        xag = _safe_float(usd.get("xag"))
        if xau is None or xag is None:
            return None
        return {"gold": 1.0 / xau, "silver": 1.0 / xag, "source": "CurrencyAPI",
                "time": str(payload.get("date") or ""),
                "usd_inr": _safe_float(usd.get("inr"))}
    except Exception as error:
        log.info("metals: currency-api failed: %s", error)
        return None


def _consensus(values: list[float]) -> float | None:
    """Average of the closest pair (3 sources), plain average (2), value (1)."""
    clean = sorted(value for value in values if value)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return (clean[0] + clean[1]) / 2.0
    if clean[1] - clean[0] < clean[2] - clean[1]:
        return (clean[0] + clean[1]) / 2.0
    return (clean[1] + clean[2]) / 2.0


def _usd_inr() -> tuple[float | None, str]:
    """USD→INR from exchangerate-api (free, no key), else currency-api."""
    try:
        resp = _quote_session().get("https://api.exchangerate-api.com/v4/latest/USD",
                                    timeout=config.HTTP_TIMEOUT)
        if resp.ok:
            rate = _safe_float((resp.json().get("rates") or {}).get("INR"))
            if rate:
                return rate, "ExchangeRate-API"
    except Exception as error:
        log.info("metals: exchangerate-api failed: %s", error)
    return None, ""


def get_metals() -> dict:
    """Consensus gold/silver USD prices + per-source table + USD/INR.

    Returns {"gold", "silver", "ratio", "sources": [...], "updated",
    "usd_inr"} with None prices when every source is down (callers show
    the manual-entry fallback instead of blanking).
    """
    now = time.time()
    cached = _METALS_CACHE.get("all")
    if cached and now - cached["timestamp"] < _METALS_TTL:
        return cached["data"]
    results = [entry for entry in (_coinpaprika(), _currency_api(), _coingecko()) if entry]
    gold = _consensus([entry["gold"] for entry in results])
    silver = _consensus([entry["silver"] for entry in results])
    usd_inr, fx_source = _usd_inr()
    if usd_inr is None:
        usd_inr = next((entry["usd_inr"] for entry in results if entry.get("usd_inr")), None)
        fx_source = "CurrencyAPI" if usd_inr else ""
    data = {
        "gold": round(gold, 2) if gold else None,
        "silver": round(silver, 2) if silver else None,
        "ratio": round(gold / silver, 2) if gold and silver else None,
        "usd_inr": round(usd_inr, 2) if usd_inr else None,
        "fx_source": fx_source,
        "sources": [
            {"source": entry["source"], "gold": round(entry["gold"], 2),
             "silver": round(entry["silver"], 2), "time": entry.get("time") or ""}
            for entry in results
        ],
        "updated": datetime.now().strftime("%H:%M:%S"),
    }
    _METALS_CACHE["all"] = {"timestamp": now, "data": data}
    return data
