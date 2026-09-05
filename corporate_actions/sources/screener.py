"""screener.in enrichment: paced fetching + caching over the pure parsers.

screener.in rate-limits aggressively, so requests are serialised and paced,
with a simple circuit breaker that pauses enrichment for 10 minutes after a
few consecutive failures so a blocked/rate-limited screener.in never slows
the movement screens down repeatedly.

All HTML parsing lives in screener_parsing.py (pure, no network); this module
only owns the fetch pacing, the circuit breaker, the sector-P/E cache and the
public API (get_sector_pe / parse_screener_fundamentals) that wires pages into
the parsers.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote

from .http import _session
import asyncio
try:
    import httpx
except Exception:
    httpx = None
from .screener_parsing import (
    parse_company_id,
    parse_company_name,
    parse_competitors,
    parse_page,
    parse_sector_pe_table,
)

log = logging.getLogger(__name__)

_screener_lock = threading.Lock()
_last_screener_req = 0.0
_screener_fail_count = 0
_screener_blocked_until = 0.0
_SCREENER_INTERVAL = 0.05  # seconds between screener.in requests
_SCREENER_MAX_FAILS = 5  # consecutive failures before pausing
_SCREENER_BLOCK_SECONDS = 600  # pause enrichment for 10 minutes when blocked


def _screener_get(url: str) -> str | None:
    """Paced, rate-limit-safe GET of a screener.in page."""
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
        response = _session().get(url, timeout=3.0)
        response.raise_for_status()
        text = response.text
    except Exception as error:
        log.info("screener.in fetch failed for %s - %s", url, error)
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


async def _screener_get_async(url: str) -> str | None:
    """Async paced GET for screener.in pages. Uses httpx when available."""
    global _last_screener_req, _screener_fail_count, _screener_blocked_until
    now = time.time()
    # use asyncio.Lock equivalent behavior via running in thread for now
    # as screener.in expects serialised requests across the process.
    with _screener_lock:
        if now < _screener_blocked_until:
            return None
        wait = _last_screener_req + _SCREENER_INTERVAL - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_screener_req = time.time()
    try:
        if httpx is None:
            # fallback to sync
            response = _session().get(url, timeout=3.0)
            response.raise_for_status()
            text = response.text
        else:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                text = r.text
    except Exception as error:
        log.info("screener.in fetch failed for %s - %s", url, error)
        with _screener_lock:
            _screener_fail_count += 1
            if _screener_fail_count >= _SCREENER_MAX_FAILS:
                _screener_blocked_until = time.time() + _SCREENER_BLOCK_SECONDS
                _screener_fail_count = 0
                log.warning("screener.in appears blocked - pausing enrichment for %ss", _SCREENER_BLOCK_SECONDS)
        return None
    with _screener_lock:
        _screener_fail_count = 0
    return text


_sector_pe_cache: dict = {}
_SECTOR_PE_CACHE_SECONDS = 86400  # 24 hours - sectors change rarely
_SECTOR_PE_RETRY_CACHE_SECONDS = 600  # 10 min when the fetch failed, so we retry soon


def get_sector_pe(slug: str) -> float | None:
    """Average P/E of a screener.in sector, from its constituent list."""
    slug = (slug or "").strip()
    if not slug:
        return None
    now = time.time()
    cached = _sector_pe_cache.get(slug)
    if cached and now - cached["timestamp"] < cached.get("time_to_live", _SECTOR_PE_CACHE_SECONDS):
        return cached["data"]
    sector_pe = None
    page = _screener_get(f"https://www.screener.in{slug}")
    if page:
        sector_pe = parse_sector_pe_table(page)
    # A failed/empty fetch is cached only briefly so a transient screener.in
    # outage (or the 10-min circuit breaker) doesn't suppress the sector P/E
    # for a full day.
    _sector_pe_cache[slug] = {
        "timestamp": now,
        "data": sector_pe,
        "time_to_live": _SECTOR_PE_CACHE_SECONDS if sector_pe else _SECTOR_PE_RETRY_CACHE_SECONDS,
    }
    return sector_pe


async def get_sector_pe_async(slug: str) -> float | None:
    slug = (slug or "").strip()
    if not slug:
        return None
    now = time.time()
    cached = _sector_pe_cache.get(slug)
    if cached and now - cached["timestamp"] < cached.get("time_to_live", _SECTOR_PE_CACHE_SECONDS):
        return cached["data"]
    sector_pe = None
    page = await _screener_get_async(f"https://www.screener.in{slug}")
    if page:
        sector_pe = parse_sector_pe_table(page)
    _sector_pe_cache[slug] = {
        "timestamp": now,
        "data": sector_pe,
        "time_to_live": _SECTOR_PE_CACHE_SECONDS if sector_pe else _SECTOR_PE_RETRY_CACHE_SECONDS,
    }
    return sector_pe


_competitors_cache: dict = {}
_COMPETITORS_CACHE_SECONDS = 86400  # 24 hours - peer sets change slowly


def get_competitors(symbol: str, limit: int = 8) -> list[dict]:
    """Top peers by market cap from screener.in (Indian stocks only).

    Two paced fetches: the company page (to find the numeric company id),
    then the /api/company/{id}/peers/ table parsed by
    screener_parsing.parse_competitors. Cached 24h. Returns [] on any
    failure or when the symbol is not on screener.in.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return []
    now = time.time()
    cached = _competitors_cache.get(symbol)
    if cached and now - cached["timestamp"] < _COMPETITORS_CACHE_SECONDS:
        return cached["data"]
    peers: list[dict] = []
    company_page = _screener_get(f"https://www.screener.in/company/{quote(symbol)}/")
    if company_page:
        company_id = parse_company_id(company_page)
        if company_id:
            peers_page = _screener_get(f"https://www.screener.in/api/company/{company_id}/peers/")
            if peers_page:
                peers = parse_competitors(peers_page)
    _competitors_cache[symbol] = {"timestamp": now, "data": peers}
    return peers[:limit]


async def get_competitors_async(symbol: str, limit: int = 8) -> list[dict]:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return []
    now = time.time()
    cached = _competitors_cache.get(symbol)
    if cached and now - cached["timestamp"] < _COMPETITORS_CACHE_SECONDS:
        return cached["data"]
    peers: list[dict] = []
    company_page = await _screener_get_async(f"https://www.screener.in/company/{quote(symbol)}/")
    if company_page:
        company_id = parse_company_id(company_page)
        if company_id:
            peers_page = await _screener_get_async(f"https://www.screener.in/api/company/{company_id}/peers/")
            if peers_page:
                peers = parse_competitors(peers_page)
    _competitors_cache[symbol] = {"timestamp": now, "data": peers}
    return peers[:limit]


def parse_screener_fundamentals(symbol: str) -> dict | None:
    """Best-effort screener.in enrichment for one symbol.

    Fetches the company page (paced + circuit-broken) and delegates every
    parse to screener_parsing.parse_page. Resolves the sector P/E - the only
    piece that needs a second network fetch - and strips the internal
    'sector_slug' key before returning.
    """
    page = _screener_get(f"https://www.screener.in/company/{quote(symbol)}/")
    if not page:
        return None
    parsed = parse_page(page)
    if not parsed:
        return None
    slug = parsed.pop("sector_slug", None)
    if slug:
        parsed["sector_pe"] = get_sector_pe(slug)
    return parsed or None


async def parse_screener_fundamentals_async(symbol: str) -> dict | None:
    page = await _screener_get_async(f"https://www.screener.in/company/{quote(symbol)}/")
    if not page:
        return None
    parsed = parse_page(page)
    if not parsed:
        return None
    slug = parsed.pop("sector_slug", None)
    if slug:
        parsed["sector_pe"] = await get_sector_pe_async(slug)
    return parsed or None
