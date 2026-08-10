"""Stock list and corporate action fetchers for NSE and BSE India.

Each function raises `SourceError` with a human-readable message on failure,
so callers can surface warnings in the UI while keeping the app running.
"""
import csv
import html
import io
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from time import mktime, strptime
from urllib.parse import quote, quote_plus
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
    """Normalise date strings (e.g. '06-Aug-2026', '06-AUG-2026', '2026-08-06T00:00:00') to ISO '2026-08-06'."""
    val = str(value or "").strip()
    if not val or val == "-":
        return "-"
    if "T" in val:
        val = val.split("T")[0]
    elif " " in val:
        val = val.split()[0]

    for fmt in (
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y%m%d",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(val.title(), fmt).date().isoformat()
        except ValueError:
            pass
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except ValueError:
            pass
    return val


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

_tls = threading.local()


def _quote_session() -> requests.Session:
    """A keep-alive session per thread (big speedup for bulk lookups)."""
    sess = getattr(_tls, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": config.USER_AGENT})
        _tls.sess = sess
    return sess


def _clean_company_name(raw_name: str) -> str:
    """Clean raw company names from Yahoo Finance (e.g., 'PGINVIT.NS,0P0001MGQ9...')."""
    if not raw_name:
        return ""
    if "," in raw_name and (".NS" in raw_name or ".BO" in raw_name):
        raw_name = raw_name.split(",")[0]
    return raw_name.removesuffix(".NS").removesuffix(".BO").strip()


def get_quote(exchange: str, symbol: str) -> dict | None:
    """Return {'price', 'prev_close', 'change_pct', 'currency', 'name'} or None."""
    exchange = exchange.upper()
    symbol = symbol.upper().removesuffix(".NS").removesuffix(".BO").strip()
    now = time.time()
    cached = _quote_cache.get((exchange, symbol))
    if cached and now - cached["ts"] < _QUOTE_TTL:
        log.debug("quote cache hit for %s:%s", exchange, symbol)
        return cached["data"]

    suffix = ".BO" if exchange == "BSE" else ".NS"
    hosts = [
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    ]
    meta = None
    for host in hosts:
        url = (
            f"{host}/v8/finance/chart/{symbol}{suffix}"
            "?range=1d&interval=1d"
        )
        try:
            resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            res = resp.json()
            if "chart" in res and "result" in res["chart"] and res["chart"]["result"]:
                meta = res["chart"]["result"][0]["meta"]
                if meta:
                    break
        except Exception as exc:
            log.debug("Quote lookup attempt failed on %s for %s:%s - %s", host, exchange, symbol, exc)
            continue

    if not meta:
        log.info("quote lookup failed for %s:%s (Yahoo %s)", exchange, symbol, suffix)
        return None

    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    name = _clean_company_name(meta.get("longName") or meta.get("shortName") or "")
    data = {
        "price": price,
        "prev_close": prev,
        "change_pct": ((price - prev) / prev * 100) if (price is not None and prev) else None,
        "currency": meta.get("currency", "INR"),
        "name": name,
    }
    _quote_cache[(exchange, symbol)] = {"ts": now, "data": data}
    log.debug("quote fetched for %s:%s", exchange, symbol)
    return data


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
        log.info("NSE stock list loaded: %d equities", len(stocks))
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
    """Case-insensitive substring search of the NSE list by symbol or company name."""
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


# Curated rights-offer windows.
#
# The NSE/BSE corporate-action feeds announce a rights issue with ex-date /
# record-date but NEVER the subscription (offer) window - those dates come
# only from the offer document (RHP/LOF) filed later. So we keep a small map
# of currently-active rights issues here, symbol -> (offer open, offer close)
# ISO dates, and attach them to the records below. Add new live rights
# issues here as they are announced; stale entries are harmless (the block
# only shows the window while both dates are in the future or one is recent).
RIGHTS_OFFER_WINDOWS: dict[str, tuple[str, str]] = {
    "GENESYS": ("2026-08-14", "2026-08-21"),
}


def _attach_rights_windows(records: list[dict]) -> list[dict]:
    """Attach rights offer-window dates to rights records when known.

    Records may already carry structured fields (e.g. if a feed adds
    rightsStartDate/rightsEndDate later) - those win. Otherwise the curated
    RIGHTS_OFFER_WINDOWS map fills the gap.
    """
    def _has(value) -> bool:
        return bool(value) and str(value).strip() not in ("", "-")

    today = config.today_ist()
    for r in records:
        if action_type(r.get("subject")) != "rights":
            continue
        if _has(r.get("rights_start")) and _has(r.get("rights_end")):
            continue
        win = RIGHTS_OFFER_WINDOWS.get((r.get("symbol") or "").upper())
        if not win:
            continue
        try:
            start = date.fromisoformat(win[0])
            end = date.fromisoformat(win[1])
        except (TypeError, ValueError):
            continue
        # Only attach to the CURRENT issue. A rights record's ex/record date
        # sits days-weeks BEFORE its offer window, so an ancient historical
        # rights record of the same symbol (ex-date far from the window) must
        # never receive this window - and a window long past is stale too.
        ex = None
        if r.get("ex_date") and str(r["ex_date"]).strip() not in ("", "-"):
            try:
                ex = date.fromisoformat(str(r["ex_date"]))
            except (TypeError, ValueError):
                ex = None
        if ex is not None and abs((ex - start).days) > 45:
            continue
        if abs((end - today).days) > 90:
            continue
        r["rights_start"], r["rights_end"] = win
    return records


def get_nse_corporate_actions(symbol: str | None = None) -> list[dict]:
    """Return corporate actions announced on NSE, normalised records.

    When `symbol` is given, the NSE API returns the full history for that
    specific symbol (the unfiltered feed only returns ~20 most-recent
    records, which usually misses most watchlist stocks).
    """
    url = config.NSE_ACTIONS_URL
    params = {}
    if symbol:
        params["symbol"] = symbol
    try:
        resp = _session().get(url, params=params, timeout=config.HTTP_TIMEOUT)
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
                "bc_start": _parse_nse_date(_pick(item, "bcStartDate", default="-")),
                "bc_end": _parse_nse_date(_pick(item, "bcEndDate", default="-")),
                "rights_start": _parse_nse_date(
                    _pick(item, "rightsStartDate", "rightsSubStartDate", "offerStartDate", default="-")
                ),
                "rights_end": _parse_nse_date(
                    _pick(item, "rightsEndDate", "rightsSubEndDate", "offerEndDate", default="-")
                ),
                "face_value": _pick(item, "faceVal"),
                "isin": _pick(item, "isin"),
                "series": _pick(item, "series"),
            }
        )
    _attach_rights_windows(records)
    log.info("NSE corporate actions fetched: %d record(s) (symbol=%s)", len(records), symbol or "all")
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
    log.info("BSE stock list loaded: %d equities", len(stocks))
    return stocks


def get_bse_corporate_actions() -> list[dict]:
    """Return BSE corporate actions for the configured lookback window."""
    today = config.today_ist()
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
                "rights_start": _parse_nse_date(
                    _pick(item, "RightsStartDate", "rightsStartDate", "OfferStartDate", "offerStartDate", default="-")
                ),
                "rights_end": _parse_nse_date(
                    _pick(item, "RightsEndDate", "rightsEndDate", "OfferEndDate", "offerEndDate", default="-")
                ),
                "face_value": _pick(item, "FaceValue", "faceVal"),
                "isin": _pick(item, "ISIN", "isin"),
                "series": _pick(item, "Series", "series"),
            }
        )
    _attach_rights_windows(records)
    log.info("BSE corporate actions fetched: %d record(s)", len(records))
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
    # Don't cache an empty result: a transient outage would otherwise make the
    # bot report "no news" for the full 10 minutes after sources recover.
    if items:
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


# ----------------------------------------------------------------- movers
# Intraday movement screen. Universe = NSE index constituents from the public
# NSE archives CSV (NIFTY 100 by default, NIFTY 500 opt-in). Per-symbol
# movement comes from Yahoo 5-minute bars over the trailing window.

_universe_cache: dict = {}
_UNIVERSE_TTL = 86400  # 24h - index constituents change rarely

_INDEX_CSV = {
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}


def get_index_universe(index: str = "nifty100") -> list[str]:
    """Return symbols for an NSE index, cached 24h. Empty list on failure."""
    key = (index or "nifty100").lower()
    url = _INDEX_CSV["nifty500"] if key in ("all", "500", "nifty500") else _INDEX_CSV["nifty100"]
    now = time.time()
    cached = _universe_cache.get(url)
    if cached and now - cached["ts"] < _UNIVERSE_TTL:
        log.debug("index universe cache hit for %s (%d symbols)", key, len(cached["data"]))
        return cached["data"]
    symbols = []
    try:
        resp = _session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        if text.startswith("\ufeff"):
            text = text[1:]
        for row in csv.DictReader(io.StringIO(text)):
            sym = (row.get("Symbol") or "").strip()
            if sym:
                symbols.append(sym)
        log.info("index universe %s loaded fresh: %d symbols", key, len(symbols))
    except Exception as exc:
        log.warning("NSE index universe unavailable (%s): %s", index, exc)
        symbols = []
    # Only cache a successful (non-empty) load. Caching an empty list for 24h
    # after one transient failure would silently make /movers and /harmonic
    # scans return nothing for the rest of the day.
    if symbols:
        _universe_cache[url] = {"ts": now, "data": symbols}
    return symbols


_intraday_cache: dict = {}
_INTRADAY_TTL = 60  # seconds


def get_intraday_change(exchange: str, symbol: str, period_minutes: int) -> dict | None:
    """% move over the trailing window using Yahoo 5-minute bars, cached.

    period_minutes <= 0 means "today" (vs the previous close). Returns
    {'price', 'change_pct', 'period_minutes', 'name'} or None.
    """
    key = (exchange.upper(), symbol.upper(), int(period_minutes))
    now = time.time()
    cached = _intraday_cache.get(key)
    if cached and now - cached["ts"] < _INTRADAY_TTL:
        log.debug("intraday cache hit for %s:%s", exchange, symbol)
        return cached["data"]
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=5m"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        price = meta.get("regularMarketPrice")
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
        closes = (quotes[0] or {}).get("close") or []
        name = meta.get("longName") or meta.get("shortName") or ""
        if price is None:
            data = None
        elif period_minutes <= 0:
            prev = (
                meta.get("chartPreviousClose")
                or meta.get("previousClose")
                or (closes[0] if closes else None)
            )
            if prev:
                data = {
                    "price": price,
                    "change_pct": (price / prev - 1) * 100,
                    "period_minutes": 0,
                    "name": name,
                }
        else:
            cutoff = now - period_minutes * 60
            base = None
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                if t >= cutoff:
                    base = c
                    break
            if base is None:
                base = closes[0] if closes else None
            if base:
                data = {
                    "price": price,
                    "change_pct": (price / base - 1) * 100,
                    "period_minutes": period_minutes,
                    "name": name,
                }
    except Exception as exc:
        log.info("intraday change failed for %s:%s - %s", exchange, symbol, exc)
    _intraday_cache[key] = {"ts": now, "data": data}
    log.debug("intraday change fetched for %s:%s (%s)", exchange, symbol, "ok" if data else "no data")
    return data


_daily_cache: dict = {}
_DAILY_TTL = 300  # seconds - daily moves change slowly


def get_daily_change(exchange: str, symbol: str, days: int) -> dict | None:
    """% move over the trailing N-day window using Yahoo daily bars, cached.

    days=1 means vs the previous close ("today"). Returns
    {'price', 'change_pct', 'days', 'name'} or None.
    """
    days = max(1, int(days))
    key = (exchange.upper(), symbol.upper(), "d", days)
    now = time.time()
    cached = _daily_cache.get(key)
    if cached and now - cached["ts"] < _DAILY_TTL:
        log.debug("daily change cache hit for %s:%s (%d days)", exchange, symbol, days)
        return cached["data"]
    if days <= 1:
        rng = "1d"
    elif days <= 5:
        rng = "5d"
    elif days <= 30:
        rng = "1mo"
    elif days <= 90:
        rng = "3mo"
    elif days <= 180:
        rng = "6mo"
    else:
        rng = "1y"
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={rng}&interval=1d"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        price = meta.get("regularMarketPrice")
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
        closes = (quotes[0] or {}).get("close") or []
        if price is None:  # market closed - fall back to the last close
            for c in reversed(closes):
                if c is not None:
                    price = c
                    break
        name = meta.get("longName") or meta.get("shortName") or ""
        if days <= 1:
            base = meta.get("chartPreviousClose") or meta.get("previousClose")
        else:
            cutoff = now - days * 86400
            base = None
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                if t >= cutoff:
                    base = c
                    break
            if base is None:
                base = next((c for c in closes if c is not None), None)
        if price and base:
            data = {
                "price": price,
                "change_pct": (price / base - 1) * 100,
                "days": days,
                "name": name,
            }
    except Exception as exc:
        log.info("daily change failed for %s:%s - %s", exchange, symbol, exc)
    _daily_cache[key] = {"ts": now, "data": data}
    log.debug("daily change fetched for %s:%s (%d days)", exchange, symbol, days)
    return data


# ------------------------------------------------------------- OHLC bars
# Raw candlestick series for a symbol/timeframe (used by the harmonic-pattern
# scanner). Yahoo chart endpoint, cached a short while.

_ohlc_cache: dict = {}
_OHLC_TTL = 120  # seconds

OHLC_TIMEFRAMES = {
    "5m": ("5m", "1d"),
    "15m": ("15m", "5d"),
    "30m": ("30m", "1mo"),
    "1h": ("1h", "3mo"),
    "4h": ("4h", "6mo"),
    "1d": ("1d", "2y"),
    "1w": ("1wk", "5y"),
    "1mo": ("1mo", "10y"),
}

_HFT_LADDER = {
    "5m": "15m",
    "15m": "1h",
    "30m": "4h",
    "1h": "4h",
    "4h": "1d",
    "1d": "1w",
    "1w": "1mo",
}


def get_ohlc(exchange: str, symbol: str, timeframe: str = "1d") -> dict | None:
    """Return OHLC bars for a symbol/timeframe via Yahoo chart, cached.

    Returns {'timestamp','open','high','low','close','volume','interval',
    'name','exchange','symbol','timeframe'} with the arrays aligned to the
    same bars, or None on any failure. Incomplete leading/trailing bars are
    dropped.
    """
    timeframe = (timeframe or "1d").lower()
    if timeframe not in OHLC_TIMEFRAMES:
        log.info("ohlc: unknown timeframe %r for %s:%s — returning None", timeframe, exchange, symbol)
        return None
    interval, rng = OHLC_TIMEFRAMES[timeframe]
    key = (exchange.upper(), symbol.upper(), interval)
    now = time.time()
    cached = _ohlc_cache.get(key)
    if cached and now - cached["ts"] < _OHLC_TTL:
        log.debug("ohlc cache hit for %s:%s (%s)", exchange, symbol, interval)
        return cached["data"]
    suffix = ".BO" if exchange.upper() == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        f"?range={rng}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
        q = quotes[0] or {}
        opens, highs, lows, closes, vols = q.get("open") or [], q.get("high") or [], \
            q.get("low") or [], q.get("close") or [], q.get("volume") or []
        rows = []
        for i in range(len(ts)):
            if i >= len(opens) or i >= len(highs) or i >= len(lows) or i >= len(closes):
                break
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            if None in (o, h, l, c):
                continue
            v = vols[i] if i < len(vols) and vols[i] is not None else 0
            rows.append((ts[i], o, h, l, c, v))
        if not rows:
            data = None
        else:
            data = {
                "timestamp": [r[0] for r in rows],
                "open": [r[1] for r in rows],
                "high": [r[2] for r in rows],
                "low": [r[3] for r in rows],
                "close": [r[4] for r in rows],
                "volume": [r[5] for r in rows],
                "interval": interval,
                "timeframe": timeframe,
                "name": meta.get("longName") or meta.get("shortName") or symbol,
                "exchange": exchange.upper(),
                "symbol": symbol.upper(),
            }
            log.info("ohlc: %d %s bars for %s:%s", len(rows), interval, exchange, symbol)
    except Exception as exc:
        log.info("ohlc failed for %s:%s (%s) - %s", exchange, symbol, interval, exc)
    _ohlc_cache[key] = {"ts": now, "data": data}
    return data


_index_ohlc_cache: dict = {}
_INDEX_OHLC_TTL = 180  # seconds


def get_index_ohlc(index_symbol: str, range_: str = "6mo",
                   interval: str = "1d") -> dict | None:
    """Return OHLC bars for a Yahoo index symbol (e.g. ^NSEI, ^INDIAVIX).

    Index symbols carry no exchange suffix, so this bypasses the .NS/.BO
    logic in get_ohlc. Same dict shape as get_ohlc. Cached briefly.
    """
    index_symbol = (index_symbol or "").strip()
    if not index_symbol:
        return None
    key = (index_symbol.upper(), range_, interval)
    now = time.time()
    cached = _index_ohlc_cache.get(key)
    if cached and now - cached["ts"] < _INDEX_OHLC_TTL:
        log.debug("index ohlc cache hit for %s", index_symbol)
        return cached["data"]
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{index_symbol}"
        f"?range={range_}&interval={interval}&includePrePost=false"
    )
    data = None
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res.get("meta") or {}
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
        q = quotes[0] or {}
        opens, highs, lows, closes, vols = q.get("open") or [], q.get("high") or [], \
            q.get("low") or [], q.get("close") or [], q.get("volume") or []
        rows = []
        for i in range(len(ts)):
            if i >= len(opens) or i >= len(highs) or i >= len(lows) or i >= len(closes):
                break
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            if None in (o, h, l, c):
                continue
            v = vols[i] if i < len(vols) and vols[i] is not None else 0
            rows.append((ts[i], o, h, l, c, v))
        if rows:
            data = {
                "timestamp": [r[0] for r in rows],
                "open": [r[1] for r in rows],
                "high": [r[2] for r in rows],
                "low": [r[3] for r in rows],
                "close": [r[4] for r in rows],
                "volume": [r[5] for r in rows],
                "interval": interval,
                "timeframe": interval,
                "name": meta.get("longName") or meta.get("shortName") or index_symbol,
                "exchange": "IDX",
                "symbol": index_symbol,
            }
            log.info("index ohlc: %d %s bars for %s", len(rows), interval, index_symbol)
    except Exception as exc:
        log.info("index ohlc failed for %s - %s", index_symbol, exc)
    _index_ohlc_cache[key] = {"ts": now, "data": data}
    return data


# ------------------------------------------------------------- fundamentals
# Stock fundamentals for the movement screens. Two public sources, both cached
# 24h (fundamentals change slowly):
#   * Yahoo quoteSummary (needs a cookie + crumb) for price, 52-week high/low,
#     P/E, dividend yield, debt/equity and sector.
#   * screener.in for sector P/E and promoter/FII/DII holdings. screener.in
#     rate-limits aggressively, so those requests are serialised and paced;
#     failures are skipped and the affected fields simply omitted.

_fund_cache: dict = {}
_FUND_TTL = 86400  # 24 hours
_FUND_RETRY_TTL = 1800  # 30 min when the screener.in part is still missing
FUND_MAX_ROWS = 40  # rows enriched with the slow screener.in part per command

_screener_lock = threading.Lock()
_last_screener_req = 0.0
_screener_fail_count = 0
_screener_blocked_until = 0.0
_SCREENER_INTERVAL = 0.05  # seconds between screener.in requests
_SCREENER_MAX_FAILS = 5  # consecutive failures before pausing
_SCREENER_BLOCK_SECONDS = 600  # pause enrichment for 10 minutes when blocked


def _screener_get(url: str) -> str | None:
    """Paced, rate-limit-safe GET of a screener.in page.

    A simple circuit breaker pauses enrichment for 10 minutes after a few
    consecutive failures so a blocked/rate-limited screener.in never slows the
    movement screens down repeatedly.
    """
    global _last_screener_req, _screener_fail_count, _screener_blocked_until
    now = time.time()
    with _screener_lock:
        if now < _screener_blocked_until:
            return None
        wait = _last_screener_req + _SCREENER_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_screener_req = time.time()
    try:
        resp = _session().get(url, timeout=3.0)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        log.info("screener.in fetch failed for %s - %s", url, exc)
        with _screener_lock:
            _screener_fail_count += 1
            if _screener_fail_count >= _SCREENER_MAX_FAILS:
                _screener_blocked_until = time.time() + _SCREENER_BLOCK_SECONDS
                _screener_fail_count = 0
                log.warning(
                    "screener.in appears blocked - pausing enrichment for %ss",
                    _SCREENER_BLOCK_SECONDS,
                )
        return None
    with _screener_lock:
        _screener_fail_count = 0
    return text


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
            resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
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


_sector_pe_cache: dict = {}
_SECTOR_PE_TTL = 86400  # 24 hours - sectors change rarely
_SECTOR_PE_RETRY_TTL = 600  # 10 min when the fetch failed, so we retry soon


def get_sector_pe(slug: str) -> float | None:
    """Average P/E of a screener.in sector, from its constituent list."""
    slug = (slug or "").strip()
    if not slug:
        return None
    now = time.time()
    cached = _sector_pe_cache.get(slug)
    if cached and now - cached["ts"] < cached.get("ttl", _SECTOR_PE_TTL):
        return cached["data"]
    pe = None
    page = _screener_get(f"https://www.screener.in{slug}")
    if page:
        table = re.search(r"<table[^>]*>(.*?)</table>", page, re.S)
        if table:
            values = []
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S)[1:]:
                cells = [
                    re.sub(r"<[^>]+>|\s+", " ", c).strip()
                    for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
                ]
                if len(cells) >= 4 and cells[3]:
                    try:
                        v = float(cells[3].replace(",", ""))
                        if v > 0:
                            values.append(v)
                    except ValueError:
                        continue
            if values:
                pe = round(sum(values) / len(values), 1)
    # A failed/empty fetch is cached only briefly so a transient screener.in
    # outage (or the 10-min circuit breaker) doesn't suppress the sector P/E
    # for a full day.
    _sector_pe_cache[slug] = {
        "ts": now,
        "data": pe,
        "ttl": _SECTOR_PE_TTL if pe else _SECTOR_PE_RETRY_TTL,
    }
    return pe


def _parse_screener_fundamentals(symbol: str) -> dict | None:
    """Best-effort ratios (P/E, Div, D/E, 52W range, ROCE, ROE, Market Cap) + holding from screener.in."""
    page = _screener_get(f"https://www.screener.in/company/{quote(symbol)}/")
    if not page:
        return None
    out = {}
    m = re.search(r'<p class="sub">(.*?)</p>', page, re.S)
    if m:
        for link in re.finditer(
            r'<a href="(/market/[^"]+)"[^>]*title="Sector">(.*?)</a>',
            m.group(1),
            re.S,
        ):
            out["sector"] = html.unescape(
                re.sub(r"<[^>]+>|\s+", " ", link.group(2)).strip()
            )
            out["sector_pe"] = get_sector_pe(link.group(1))
            break

    # Parse top ratios block (Stock P/E, Market Cap, Dividend Yield, Debt to equity, High/Low, ROCE, ROE)
    i = page.find('id="top-ratios"')
    if i > 0:
        chunk = page[i:i + 3000]
        for li in re.findall(r'<li[^>]*>(.*?)</li>', chunk, re.S):
            name_m = re.search(r'<span class="name"[^>]*>(.*?)</span>', li, re.S)
            num_m = re.findall(r'<span class="number"[^>]*>(.*?)</span>', li, re.S)
            if name_m and num_m:
                name = re.sub(r'<[^>]+>|\s+', ' ', name_m.group(1)).strip().lower()
                vals = [
                    re.sub(r'<[^>]+>|\s+|,|₹', '', n.group(1) if hasattr(n, 'group') else str(n)).strip()
                    for n in num_m
                ]
                if vals and vals[0]:
                    try:
                        if 'stock p/e' in name or name == 'p/e':
                            out['pe'] = float(vals[0])
                        elif 'dividend yield' in name:
                            out['div_yield'] = float(vals[0])
                        elif 'debt to equity' in name:
                            out['debt_to_equity'] = float(vals[0])
                        elif 'roce' in name:
                            out['roce'] = float(vals[0])
                        elif 'roe' in name:
                            out['roe'] = float(vals[0])
                        elif 'market cap' in name:
                            out['market_cap'] = float(vals[0])
                        elif 'high / low' in name or 'high/low' in name:
                            if len(vals) >= 2 and vals[1]:
                                out['wk52_high'] = float(vals[0])
                                out['wk52_low'] = float(vals[1])
                    except (ValueError, IndexError):
                        pass

    def _parse_chg(curr_str, prev_str):
        try:
            c = float(re.sub(r"[^\d\.-]", "", curr_str))
            p = float(re.sub(r"[^\d\.-]", "", prev_str))
            diff = round(c - p, 2)
            if diff > 0:
                return f"{curr_str} (\u25b2+{diff:.2f}%)"
            elif diff < 0:
                return f"{curr_str} (\u25bc{diff:.2f}%)"
            else:
                return curr_str
        except Exception:
            return curr_str

    i = page.find('<div id="quarterly-shp"')
    j = page.find('<div id="yearly-shp"')
    seg = page[i:j] if i > 0 and j > i else ""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        first = re.search(r'<td class="text">(.*?)</td>', row, re.S)
        if not first:
            continue
        label = re.sub(r"<[^>]+>|\s+", " ", first.group(1)).strip().lower()
        cells = [
            re.sub(r"<[^>]+>|\s+", " ", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if cells:
            curr = cells[-1]
            prev = cells[-2] if len(cells) >= 2 else None
            val_str = _parse_chg(curr, prev) if prev else curr
            if label.startswith("promoter"):
                out["promoter_pct"] = val_str
            elif label.startswith("fii"):
                out["fii_pct"] = val_str
            elif label.startswith("dii"):
                out["dii_pct"] = val_str
            elif label.startswith("public"):
                out["public_pct"] = val_str
    return out or None


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
        scr = _parse_screener_fundamentals(key)
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
