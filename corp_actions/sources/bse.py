"""BSE stock list and corporate actions.

NOTE: BSE's api.bseindia.com is Cloudflare-protected and may block datacenter
IPs (returns HTTP 403). It usually works from residential networks. Failures
surface as SourceError and are shown as a warning.
"""
from __future__ import annotations

import logging

import requests

from .. import config
from ..core.dates import parse_nse_date, today_ist
from .errors import SourceError
from .http import _session
from .rights import attach_rights_windows
from .types import pick

log = logging.getLogger(__name__)


def get_bse_stock_list() -> list[dict]:
    """Return list of {'symbol', 'company', 'exchange'} for BSE equities."""
    try:
        resp = _session().get(config.BSE_LIST_URL, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SourceError(f"BSE stock list request failed: {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"BSE stock list bad JSON: {exc}") from exc

    rows = data if isinstance(data, list) else data.get("Table", data.get("data", []))
    stocks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = pick(row, "ShortName", "ScripName", "Scrip_Name", "symbol", "Symbol")
        company = pick(row, "ScripName", "ShortName", "CompanyName", "company")
        code = pick(row, "ScripCode", "scripcode", "Code")
        if not symbol:
            continue
        stocks.append(
            {
                "symbol": symbol.upper(),
                "company": company,
                "exchange": "BSE",
                "code": code,
            }
        )
    if not stocks:
        raise SourceError("BSE stock list parsed but empty")
    log.info("BSE stock list loaded: %d equities", len(stocks))
    return stocks


def get_bse_corporate_actions() -> list[dict]:
    """Return BSE corporate actions for the configured lookback window."""
    from datetime import timedelta

    today = today_ist()
    start = today - timedelta(days=config.LOOKBACK_DAYS)
    params = {
        "pageno": 1,
        "strCat": 14,
        "dtStart": start.strftime("%Y%m%d"),
        "dtEnd": today.strftime("%Y%m%d"),
    }
    try:
        resp = _session().get(config.BSE_ACTIONS_URL, params=params, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SourceError(f"BSE corporate actions request failed: {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"BSE corporate actions bad JSON: {exc}") from exc

    rows = data if isinstance(data, list) else data.get("Table", data.get("data", []))
    records = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "symbol": pick(item, "ShortName", "symbol", "Symbol", default=""),
                "company": pick(item, "LongName", "CompanyName", "company"),
                "exchange": "BSE",
                "subject": pick(item, "Purpose", "subject"),
                "ex_date": parse_nse_date(pick(item, "ExDate", "exDate", default="-")),
                "record_date": parse_nse_date(pick(item, "RecDate", "recDate", default="-")),
                "announcement_date": parse_nse_date(
                    pick(item, "AnnDate", "BroadcastDate", "caBroadcastDate", default="-")
                ),
                "rights_start": parse_nse_date(
                    pick(item, "RightsStartDate", "rightsStartDate", "OfferStartDate", "offerStartDate", default="-")
                ),
                "rights_end": parse_nse_date(
                    pick(item, "RightsEndDate", "rightsEndDate", "OfferEndDate", "offerEndDate", default="-")
                ),
                "face_value": pick(item, "FaceValue", "faceVal"),
                "isin": pick(item, "ISIN", "isin"),
                "series": pick(item, "Series", "series"),
            }
        )
    attach_rights_windows(records)
    log.info("BSE corporate actions fetched: %d record(s)", len(records))
    return records
