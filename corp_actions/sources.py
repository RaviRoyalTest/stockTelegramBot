"""Stock list and corporate action fetchers for NSE and BSE India.

Each function raises `SourceError` with a human-readable message on failure,
so callers can surface warnings in the UI while keeping the app running.
"""
import csv
import io
import logging
import time
from datetime import date, datetime, timedelta
from time import mktime, strptime
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

from . import config

log = logging.getLogger(__name__)


class SourceError(Exception):
    """Raised when an upstream source cannot be reached or parsed."""


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(config.BROWSER_HEADERS)
    return session


# ---------------------------------------------------------------- utilities

# ------------------------------------------------------------------ action types
# Corporate actions are classified from the subject line into broad buckets so
# users can filter which alerts they receive (e.g. dividends only).

ACTION_TYPES = ("dividend", "bonus", "split", "rights", "buyback", "other")
TYPE_LABELS = {
    "dividend": "Dividend",
    "bonus": "Bonus",
    "split": "Split",
    "rights": "Rights",
    "buyback": "Buy-back",
    "other": "Other",
}

# Actions that INCREASE the number of shares a holder owns. Used by the
# /ca increase query (bonus + split + rights are all share-count increasing).
INCREASE_TYPES = ("bonus", "split", "rights")


def action_type(subject) -> str:
    """Classify a corporate-action subject into one of ACTION_TYPES."""
    text = (subject or "").lower()
    if "dividend" in text:
        return "dividend"
    if "bonus" in text:
        return "bonus"
    if "split" in text or "sub-division" in text or "sub division" in text:
        return "split"
    if "rights" in text or "right issue" in text:
        return "rights"
    if "buy back" in text or "buyback" in text:
        return "buyback"
    return "other"


def _parse_nse_date(value: str) -> str:
    """Normalise NSE date strings like '06-Aug-2026' to ISO '2026-08-06'."""
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return value.strip()


def _pick(obj, *keys, default=""):
    """Return the first non-empty value found among candidate keys."""
    for key in keys:
        val = obj.get(key)
        if val is not None and str(val).strip() and str(val).strip() != "-":
            return str(val).strip()
    return default


# ---------------------------------------------------------------- quotes
# NSE/BSE quote APIs are WAF-protected, so live prices come from Yahoo
# Finance (public, no key). Suffixes: .NS = NSE, .BO = BSE.

_quote_cache: dict = {}
_QUOTE_TTL = 60  # seconds


def get_quote(exchange: str, symbol: str) -> dict | None:
    """Return {'price', 'prev_close', 'change_pct', 'currency'} or None."""
    exchange = exchange.upper()
    symbol = symbol.upper()
    now = time.time()
    cached = _quote_cache.get((exchange, symbol))
    if cached and now - cached["ts"] < _QUOTE_TTL:
        return cached["data"]

    suffix = ".BO" if exchange == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=1d"
    )
    try:
        resp = requests.get(
            url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT
        )
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        data = {
            "price": price,
            "prev_close": prev,
            "change_pct": ((price - prev) / prev * 100) if (price and prev) else None,
            "currency": meta.get("currency", "INR"),
            "name": meta.get("longName") or meta.get("shortName") or "",
        }
        _quote_cache[(exchange, symbol)] = {"ts": now, "data": data}
        return data
    except Exception as exc:
        log.info(
            "quote lookup failed for %s:%s (Yahoo %s) - %s",
            exchange, symbol, suffix, exc,
        )
        return None


# ------------------------------------------------------------------- NSE

def get_nse_stock_list() -> list[dict]:
    """Return list of {'symbol', 'company', 'exchange'} for NSE equities."""
    try:
        resp = _session().get(config.NSE_STOCK_LIST_URL, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        # Strip the UTF-8 BOM if present.
        if text.startswith("\ufeff"):
            text = text[1:]
        reader = csv.DictReader(io.StringIO(text))
        stocks = []
        for row in reader:
            symbol = (row.get("SYMBOL") or "").strip()
            company = (row.get("NAME OF COMPANY") or "").strip()
            if symbol and symbol.lower() not in ("symbol", "index"):
                stocks.append(
                    {"symbol": symbol, "company": company, "exchange": "NSE"}
                )
        if not stocks:
            raise SourceError("NSE stock list parsed but empty")
        return stocks
    except SourceError:
        raise
    except requests.RequestException as exc:
        raise SourceError(f"NSE stock list request failed: {exc}") from exc
    except Exception as exc:  # csv.DictReader can raise Error
        raise SourceError(f"NSE stock list parse failed: {exc}") from exc


_nse_list_cache = None
_nse_list_cache_ts = 0.0
_NSE_LIST_TTL = 3600  # seconds


def get_nse_stock_list_cached() -> list[dict]:
    """NSE stock list with a 1h in-process cache (used by the bot's search)."""
    global _nse_list_cache, _nse_list_cache_ts
    now = time.time()
    if _nse_list_cache and now - _nse_list_cache_ts < _NSE_LIST_TTL:
        return _nse_list_cache
    _nse_list_cache = get_nse_stock_list()
    _nse_list_cache_ts = now
    return _nse_list_cache


def search_stocks(query: str, limit: int = 10) -> list[dict]:
    """Fuzzy search the NSE list by symbol or company name (case-insensitive)."""
    q = (query or "").upper()
    try:
        stocks = get_nse_stock_list_cached()
    except SourceError as exc:
        log.warning("NSE stock list unavailable for search: %s", exc)
        return []
    matches = [
        s for s in stocks
        if q in s["symbol"].upper() or q in s["company"].upper()
    ]
    return matches[:limit]


def get_nse_corporate_actions() -> list[dict]:
    """Return corporate actions announced on NSE, normalised records."""
    try:
        resp = _session().get(config.NSE_ACTIONS_URL, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SourceError(f"NSE corporate actions request failed: {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"NSE corporate actions bad JSON: {exc}") from exc

    if not isinstance(data, list):
        raise SourceError("NSE corporate actions returned unexpected payload")

    records = []
    for item in data:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "symbol": _pick(item, "symbol"),
                "company": _pick(item, "comp"),
                "exchange": "NSE",
                "subject": _pick(item, "subject"),
                "ex_date": _parse_nse_date(_pick(item, "exDate", default="-")),
                "record_date": _parse_nse_date(_pick(item, "recDate", default="-")),
                "announcement_date": _parse_nse_date(
                    _pick(item, "caBroadcastDate", default="-")
                ),
                "face_value": _pick(item, "faceVal"),
                "isin": _pick(item, "isin"),
                "series": _pick(item, "series"),
            }
        )
    return records


# ------------------------------------------------------------------- BSE
# NOTE: BSE's api.bseindia.com is Cloudflare-protected and may block
# datacenter IPs (returns HTTP 403). It usually works from residential
# networks. Failures surface as SourceError and are shown as a warning.

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
        symbol = _pick(row, "ShortName", "ScripName", "Scrip_Name", "symbol", "Symbol")
        company = _pick(row, "ScripName", "ShortName", "CompanyName", "company")
        code = _pick(row, "ScripCode", "scripcode", "Code")
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
    return stocks


def get_bse_corporate_actions() -> list[dict]:
    """Return BSE corporate actions for the configured lookback window."""
    today = date.today()
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
                "symbol": _pick(item, "ShortName", "symbol", "Symbol", default=""),
                "company": _pick(item, "LongName", "CompanyName", "company"),
                "exchange": "BSE",
                "subject": _pick(item, "Purpose", "subject"),
                "ex_date": _parse_nse_date(_pick(item, "ExDate", "exDate", default="-")),
                "record_date": _parse_nse_date(_pick(item, "RecDate", "recDate", default="-")),
                "announcement_date": _parse_nse_date(
                    _pick(item, "AnnDate", "BroadcastDate", "caBroadcastDate", default="-")
                ),
                "face_value": _pick(item, "FaceValue", "faceVal"),
                "isin": _pick(item, "ISIN", "isin"),
                "series": _pick(item, "Series", "series"),
            }
        )
    return records


# ------------------------------------------------------------------- news
# Latest news for a stock. Primary source is Google News RSS (India-relevant
# headlines for Indian tickers, no key); falls back to Yahoo Finance search
# (public, same host already used for quotes). Both are WAF-friendly.

_news_cache: dict = {}
_NEWS_TTL = 600  # seconds


def get_stock_news(exchange: str, symbol: str, limit: int = 3) -> list[dict]:
    """Return latest news items for a stock, cached for _NEWS_TTL.

    Each item is {'title', 'publisher', 'link', 'published_ts'}. Never
    raises - failures degrade to an empty list so the bot can report
    "no news right now" instead of crashing.
    """
    cache_key = (exchange.upper(), symbol.upper(), limit)
    now = time.time()
    cached = _news_cache.get(cache_key)
    if cached and now - cached["ts"] < _NEWS_TTL:
        return cached["data"]
    # Google News RSS gives India-relevant headlines for Indian tickers;
    # Yahoo search is the fallback when Google is unreachable.
    items = _google_news(symbol, limit) or _yf_news(exchange, symbol, limit)
    _news_cache[cache_key] = {"ts": now, "data": items}
    return items


def _yf_news(exchange: str, symbol: str, limit: int) -> list[dict]:
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v1/finance/search?q={symbol}{suffix}"
        f"&newsCount={limit}&quotesCount=0"
    )
    try:
        resp = requests.get(
            url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT
        )
        resp.raise_for_status()
        news = (resp.json().get("news") or [])[:limit]
        items = []
        for n in news:
            title = n.get("title")
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "publisher": n.get("publisher") or "",
                    "link": n.get("link") or "",
                    "published_ts": n.get("providerPublishTime"),
                }
            )
        return items
    except Exception as exc:
        log.info("Yahoo news failed for %s:%s - %s", exchange, symbol, exc)
        return []


def _google_news(symbol: str, limit: int) -> list[dict]:
    query = quote_plus(f"{symbol} NSE OR BSE stock India")
    url = (
        f"https://news.google.com/rss/search?q={query}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        resp = requests.get(
            url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            pub = (item.findtext("pubDate") or "").strip()
            ts = None
            try:
                ts = int(mktime(strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")))
            except (ValueError, TypeError):
                ts = None
            items.append(
                {
                    "title": title,
                    "publisher": (item.findtext("source") or "").strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "published_ts": ts,
                }
            )
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        log.info("Google News failed for %s - %s", symbol, exc)
        return []
