"""Cash-dividend history from Yahoo chart events (free, no key, no crumb).

Yahoo's /v8/finance/chart endpoint returns split-adjusted dividend events
when asked with ?events=div — the same source the reference app uses for
its Dividend History section. Cached 24h; dividends change rarely.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from .. import config
from .http import _quote_session, _throttle_chart_req

log = logging.getLogger(__name__)

_DIV_CACHE: dict = {}
_DIV_CACHE_SECONDS = 86400  # 24 hours


def get_dividends(exchange: str, symbol: str, years: int = 5) -> dict:
    """Dividend payouts for a symbol: events + annual totals.

    Returns {"dividends": [{"date": ISO, "amount": per-share}],
    "annual": [{"year", "total"}], "count", "last": {...} | None}.
    Empty dividends list (never throws) when Yahoo has no events.
    """
    base = (symbol or "").strip().upper().removesuffix(".NS").removesuffix(".BO")
    if not base:
        return {"dividends": [], "annual": [], "count": 0, "last": None}
    key = ((exchange or "NSE").upper(), base, max(1, int(years or 5)))
    now = time.time()
    cached = _DIV_CACHE.get(key)
    if cached and now - cached["timestamp"] < _DIV_CACHE_SECONDS:
        return cached["data"]
    suffix = "" if key[0] == "US" else (".BO" if key[0] == "BSE" else ".NS")
    data: dict = {"dividends": [], "annual": [], "count": 0, "last": None}
    for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        url = (f"{host}/v8/finance/chart/{base}{suffix}"
               f"?range={key[2]}y&interval=1d&events=div&includePrePost=false")
        try:
            _throttle_chart_req()
            resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            results = (resp.json().get("chart") or {}).get("result") or []
            if not results:
                continue
            events = ((results[0].get("events") or {}).get("dividends") or {})
            rows = []
            for raw in events.values():
                try:
                    amount = float(raw.get("amount"))
                except (TypeError, ValueError):
                    continue
                if amount <= 0:
                    continue
                try:
                    day = datetime.fromtimestamp(int(raw.get("date")), tz=timezone.utc).date()
                except (TypeError, ValueError):
                    continue
                rows.append({"date": day.isoformat(), "amount": round(amount, 4)})
            rows.sort(key=lambda r: r["date"], reverse=True)
            annual_map: dict[int, float] = defaultdict(float)
            for row in rows:
                annual_map[int(row["date"][:4])] += row["amount"]
            annual = [{"year": year, "total": round(total, 4)}
                      for year, total in sorted(annual_map.items(), reverse=True)]
            data = {"dividends": rows, "annual": annual, "count": len(rows),
                    "last": rows[0] if rows else None}
            break
        except Exception as error:
            log.info("dividends failed for %s on %s: %s", base, host, error)
    _DIV_CACHE[key] = {"timestamp": now, "data": data}
    return data
