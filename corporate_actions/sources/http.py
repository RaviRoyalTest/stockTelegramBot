"""Shared HTTP plumbing for all data sources.

Holds the browser-like session factory, a per-thread keep-alive quote session,
and a global request-rate limiter used to stay under Yahoo's 429 threshold.
"""
import threading
import time
import asyncio

import requests

from .. import config

# Optional async HTTP client using httpx
try:
    import httpx
except Exception:  # pragma: no cover - optional dependency
    httpx = None

_tls = threading.local()

# Shared Yahoo 429 cooldown: when ANY caller sees HTTP 429 (IP-level ban),
# every Yahoo caller backs off together via the throttles below instead of
# each failing its own symbol and hammering on. Without this, one ban
# empties entire reports (missing P/E, RSI, forecasts) for minutes.
_yahoo_cooldown_lock = threading.Lock()
_yahoo_cooldown_until = 0.0
_YAHOO_COOLDOWN_DEFAULT = 10.0  # seconds when Yahoo gives no Retry-After
_YAHOO_COOLDOWN_MAX = 60.0  # never freeze the app longer than this


def _note_yahoo_429(retry_after=None):
    """Record a Yahoo 429 so all callers cool down together.

    Honors the Retry-After response header when present, else a short
    default. Thread-safe; the latest (longest) cooldown wins.
    """
    try:
        wait = float(retry_after)
    except (TypeError, ValueError):
        wait = _YAHOO_COOLDOWN_DEFAULT
    wait = min(max(wait, 1.0), _YAHOO_COOLDOWN_MAX)
    global _yahoo_cooldown_until
    with _yahoo_cooldown_lock:
        _yahoo_cooldown_until = max(_yahoo_cooldown_until, time.time() + wait)
        log = __import__("logging").getLogger(__name__)
        log.warning("Yahoo 429 observed - shared cooldown %.0fs", wait)


def _yahoo_cooldown_sleep():
    """Sleep (outside any throttle lock) while a shared cooldown is active."""
    while True:
        with _yahoo_cooldown_lock:
            remaining = _yahoo_cooldown_until - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5.0))


async def _yahoo_cooldown_sleep_async():
    """Async version of the shared-cooldown sleep."""
    while True:
        with _yahoo_cooldown_lock:
            remaining = _yahoo_cooldown_until - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 5.0))

_fund_req_lock = threading.Lock()
_last_fund_req = 0.0
_FUND_REQ_INTERVAL = 0.15  # seconds between quoteSummary requests (Yahoo 429 guard)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(config.BROWSER_HEADERS)
    return session


def _quote_session() -> requests.Session:
    """A keep-alive session per thread (big speedup for bulk lookups)."""
    sess = getattr(_tls, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": config.USER_AGENT})
        _tls.sess = sess
    return sess


def _throttle_fund_req():
    """Enforce a minimum gap between Yahoo quoteSummary requests.

    Yahoo aggressively rate-limits (HTTP 429 "Edge: Too Many Requests"),
    which is the root cause of the missing P/E, MCap, ROCE/ROE and dividend
    yield on the movers reports. A tiny global inter-request gap plus the
    existing per-thread sessions keeps bulk fundamentals well under the
    limit even when the movers enrichment fans out across 10 threads.

    `time.time()` must be read INSIDE the lock: reading it before waiting
    makes queued threads over-sleep by their lock-wait time, and with many
    threads that compounds exponentially (each queued thread sleeps roughly
    double the previous), turning a 5-second scan into a multi-minute stall.
    """
    global _last_fund_req
    _yahoo_cooldown_sleep()
    with _fund_req_lock:
        now = time.time()
        wait = _last_fund_req + _FUND_REQ_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_fund_req = time.time()


_chart_req_lock = threading.Lock()
_last_chart_req = 0.0
_CHART_REQ_INTERVAL = 0.05  # seconds between Yahoo chart requests


def _throttle_chart_req():
    """Enforce a minimum gap between Yahoo /v8/finance/chart requests.

    The always-on sudden-move watcher scans up to 500 symbols every 3 minutes
    and the movers screens fan out across ~25 threads. With no gap, one IP
    can trip Yahoo's rate limiter - and once the IP is throttled, the
    quoteSummary endpoint (analyst forecasts for /forecast, deep
    fundamentals for /fundamentalreport) starts returning 429 too, which
    makes reports silently lose whole sections. A tiny global inter-request
    gap keeps the watcher/movers under the limit.
    """
    global _last_chart_req
    _yahoo_cooldown_sleep()
    with _chart_req_lock:
        now = time.time()
        wait = _last_chart_req + _CHART_REQ_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_chart_req = time.time()


async def _throttle_chart_req_async():
    """Async version of chart request throttling.

    Best-effort gap without a lock (threading.Lock cannot be used with
    `async with` - that raised TypeError on every call) plus the shared
    429 cooldown sleep.
    """
    global _last_chart_req
    await _yahoo_cooldown_sleep_async()
    now = time.time()
    wait = _last_chart_req + _CHART_REQ_INTERVAL - now
    if wait > 0:
        await asyncio.sleep(wait)
    _last_chart_req = time.time()


async def _throttle_fund_req_async():
    """Async version of fund request throttling (lock-free, see above)."""
    global _last_fund_req
    await _yahoo_cooldown_sleep_async()
    now = time.time()
    wait = _last_fund_req + _FUND_REQ_INTERVAL - now
    if wait > 0:
        await asyncio.sleep(wait)
    _last_fund_req = time.time()


_async_client_instance = None


def _async_client():
    """Return a shared httpx.AsyncClient or None if httpx unavailable."""
    global _async_client_instance
    if httpx is None:
        return None
    if _async_client_instance is None:
        _async_client_instance = httpx.AsyncClient(headers=config.BROWSER_HEADERS, timeout=config.HTTP_TIMEOUT)
    return _async_client_instance
