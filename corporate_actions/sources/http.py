"""Shared HTTP plumbing for all data sources.

Holds the browser-like session factory, a per-thread keep-alive quote session,
and a global request-rate limiter used to stay under Yahoo's 429 threshold.
"""
import threading
import time

import requests

from .. import config

_tls = threading.local()

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
    with _chart_req_lock:
        now = time.time()
        wait = _last_chart_req + _CHART_REQ_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_chart_req = time.time()
