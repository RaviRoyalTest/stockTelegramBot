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

_tls = threading.local()


def _quote_session() -> requests.Session:
    """A keep-alive session per thread (big speedup for bulk lookups)."""
    sess = getattr(_tls, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": config.USER_AGENT})
        _tls.sess = sess
    return sess


def get_quote(exchange: str, symbol: str) -> dict | None:
    """Return {'price', 'prev_close', 'change_pct', 'currency'} or None."""
    exchange = exchange.upper()
    symbol = symbol.upper()
    now = time.time()
    cached = _quote_cache.get((exchange, symbol))
    if cached and now - cached["ts"] < _QUOTE_TTL:
        log.debug("quote cache hit for %s:%s", exchange, symbol)
        return cached["data"]

    suffix = ".BO" if exchange == "BSE" else ".NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
        "?range=1d&interval=1d"
    )
    try:
        resp = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
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
        log.debug("quote fetched for %s:%s", exchange, symbol)
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
    log.info("NSE corporate actions fetched: %d record(s)", len(records))
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
_SCREENER_INTERVAL = 0.9  # seconds between screener.in requests
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
        resp = _session().get(url, timeout=config.HTTP_TIMEOUT)
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


def _fund_session():
    """A per-thread Yahoo session with its own cookie + crumb."""
    sess = getattr(_tls, "fund_sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": config.USER_AGENT})
        try:
            sess.get("https://fc.yahoo.com", timeout=config.HTTP_TIMEOUT)
        except Exception:
            pass
        _tls.fund_sess = sess
    crumb = getattr(_tls, "fund_crumb", "")
    if not crumb:
        try:
            resp = sess.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                timeout=config.HTTP_TIMEOUT,
            )
            if resp.status_code == 200 and resp.text.strip():
                crumb = resp.text.strip()
                _tls.fund_crumb = crumb
        except Exception as exc:
            log.info("Yahoo crumb unavailable: %s", exc)
    return sess, crumb


def _quote_summary(symbol: str) -> dict | None:
    """Yahoo quoteSummary result (summaryDetail/financialData/assetProfile)."""
    sess, crumb = _fund_session()
    if not crumb:
        return None
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{quote(symbol)}.NS"
    )
    try:
        resp = sess.get(
            url,
            params={
                "modules": "summaryDetail,financialData,assetProfile",
                "crumb": crumb,
            },
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["quoteSummary"]["result"][0]
    except Exception as exc:
        log.info("quoteSummary failed for %s - %s", symbol, exc)
        return None


_sector_pe_cache: dict = {}
_SECTOR_PE_TTL = 86400  # 24 hours - sectors change rarely


def get_sector_pe(slug: str) -> float | None:
    """Average P/E of a screener.in sector, from its constituent list."""
    slug = (slug or "").strip()
    if not slug:
        return None
    now = time.time()
    cached = _sector_pe_cache.get(slug)
    if cached and now - cached["ts"] < _SECTOR_PE_TTL:
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
    _sector_pe_cache[slug] = {"ts": now, "data": pe}
    return pe


def _parse_screener_fundamentals(symbol: str) -> dict | None:
    """Best-effort promoter/FII/DII holding + sector P/E from screener.in."""
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
        last = cells[-1] if cells else ""
        if label.startswith("promoter"):
            out["promoter_pct"] = last
        elif label.startswith("fii"):
            out["fii_pct"] = last
        elif label.startswith("dii"):
            out["dii_pct"] = last
    return out or None


def get_fundamentals(symbol: str, with_screener: bool = True) -> dict | None:
    """Return fundamentals for an NSE symbol, cached.

    Always-attempted Yahoo fields: price, wk52_high, wk52_low, pe,
    div_yield (%), debt_to_equity (ratio), sector. When with_screener is
    true (the default) the slow screener.in part (sector_pe, promoter_pct,
    fii_pct, dii_pct) is added as well. Missing keys mean the value could not
    be obtained; None is returned only when nothing at all was available.
    """
    key = symbol.strip().upper()
    now = time.time()
    cached = _fund_cache.get(key)
    if cached and now - cached["ts"] < cached["ttl"]:
        log.debug("fundamentals cache hit for %s", key)
        return cached["data"]
    out = {}
    res = _quote_summary(key)
    if res:
        sd = res.get("summaryDetail") or {}
        fd = res.get("financialData") or {}
        ap = res.get("assetProfile") or {}

        def _raw(d, k):
            v = d.get(k) or {}
            return v.get("raw") if isinstance(v, dict) else v

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
        if dy:
            out["div_yield"] = round(dy * 100, 2)
        de = _raw(fd, "debtToEquity")  # Yahoo reports percent (36.65 -> 0.37)
        if de:
            out["debt_to_equity"] = round(de / 100, 2)
        if ap.get("sector"):
            out["sector"] = ap["sector"]
    if with_screener:
        scr = _parse_screener_fundamentals(key)
        if scr:
            out.update({k: v for k, v in scr.items() if v is not None})
    data = out or None
    ttl = _FUND_TTL
    if with_screener and not (out.get("promoter_pct") or out.get("sector_pe")):
        ttl = _FUND_RETRY_TTL  # retry the rate-limited part sooner
    _fund_cache[key] = {"ts": now, "data": data, "ttl": ttl}
    log.debug(
        "fundamentals fetched for %s (screener=%s): %d field(s)",
        key, with_screener, len(out),
    )
    return data
