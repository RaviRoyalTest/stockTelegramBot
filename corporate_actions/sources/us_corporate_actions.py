"""US corporate actions from Yahoo Finance chart events (dividends/splits).

Yahoo's chart endpoint exposes a per-symbol `events` object: dividends are
keyed by ex-date timestamp (value holds `amount` + payment `date`) and splits
carry `numerator`/`denominator` with the effective date. This module turns
those events into the same normalised records the NSE/BSE sources emit, so the
poller can treat US watchlist entries exactly like Indian ones.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from .. import config
from .http import _quote_session
from .types import pick

log = logging.getLogger(__name__)

_us_actions_cache: dict = {}
_US_ACTIONS_CACHE_SECONDS = 300  # 5 minutes
_US_ACTIONS_LOOKBACK_DAYS = 30
_US_ACTIONS_LOOKAHEAD_DAYS = 90

SPLIT_BASE_DENOMINATOR = 1.0


def _iso_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()


def get_us_corporate_actions(symbol: str) -> list[dict]:
    """Return normalised corporate actions (dividend/split) for a US symbol.

    Queries the Yahoo chart endpoint per-symbol (mirrors how NSE is fetched
    per-symbol in the poller) and converts each event into the shared record
    shape: symbol/company/exchange/subject/ex_date/record_date. Only events
    within a recent lookback + lookahead window are kept so the watchlist
    stays focused on actionable items. Cached for 5 minutes.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return []
    now = time.time()
    cached = _us_actions_cache.get(symbol)
    if cached and now - cached["timestamp"] < _US_ACTIONS_CACHE_SECONDS:
        return cached["data"]

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=1y&interval=1mo&events=div,split&includePrePost=false"
    )
    actions = []
    try:
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        company = meta.get("longName") or meta.get("shortName") or symbol
        events = result.get("events") or {}
        for ex_ts, dividend in (events.get("dividends") or {}).items():
            ex_date = _iso_date(float(ex_ts))
            if not _within_window(ex_date):
                continue
            amount = dividend.get("amount")
            payment_ts = dividend.get("date")
            payment_date = _iso_date(payment_ts) if payment_ts else None
            subject = f"Dividend of ${amount:,.4f} per share" if amount is not None else "Dividend"
            record = {
                "symbol": symbol,
                "company": company,
                "exchange": "US",
                "subject": subject,
                "ex_date": ex_date,
                "record_date": payment_date,
                "announcement_date": "",
                "book_closure_start": "",
                "book_closure_end": "",
                "rights_start": "",
                "rights_end": "",
                "face_value": "",
                "isin": "",
                "series": "",
            }
            actions.append(record)
        for split_ts, split in (events.get("splits") or {}).items():
            ex_date = _iso_date(float(split_ts))
            if not _within_window(ex_date):
                continue
            numerator = split.get("numerator")
            denominator = split.get("denominator")
            ratio = f"{numerator} for {denominator}" if numerator and denominator else "split"
            record = {
                "symbol": symbol,
                "company": company,
                "exchange": "US",
                "subject": f"Stock Split - {ratio}",
                "ex_date": ex_date,
                "record_date": "",
                "announcement_date": "",
                "book_closure_start": "",
                "book_closure_end": "",
                "rights_start": "",
                "rights_end": "",
                "face_value": "",
                "isin": "",
                "series": "",
            }
            actions.append(record)
    except Exception as error:
        log.info("US corporate actions failed for %s: %s", symbol, error)

    actions.sort(key=lambda action: action.get("ex_date") or "")
    _us_actions_cache[symbol] = {"timestamp": now, "data": actions}
    return actions


def _within_window(ex_date: str) -> bool:
    """True when ex_date is inside the recent-lookback / lookahead window."""
    try:
        parsed = date.fromisoformat(ex_date)
    except (TypeError, ValueError):
        return False
    today = date.today()
    return today - timedelta(days=_US_ACTIONS_LOOKBACK_DAYS) <= parsed <= \
        today + timedelta(days=_US_ACTIONS_LOOKAHEAD_DAYS)
