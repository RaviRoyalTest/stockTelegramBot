"""Latest news for a stock: Google News RSS primary, Yahoo Finance fallback."""
from __future__ import annotations

import logging
import time
from time import mktime, strptime
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

from .. import config

log = logging.getLogger(__name__)

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
