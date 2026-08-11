"""Dispatch a (kind, value) movement period to the right Yahoo fetcher."""
from __future__ import annotations

from ..sources.universe import get_daily_change, get_intraday_change


def fetch_period_change(symbol: str, period: tuple) -> dict | None:
    """Return the %-move dict for (kind, value), e.g. ('intraday', 60)."""
    kind, value = period
    if kind == "intraday":
        return get_intraday_change("NSE", symbol, value)
    return get_daily_change("NSE", symbol, value)
