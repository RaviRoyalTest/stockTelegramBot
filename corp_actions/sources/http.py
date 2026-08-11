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
    """
    global _last_fund_req
    now = time.time()
    with _fund_req_lock:
        wait = _last_fund_req + _FUND_REQ_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_fund_req = time.time()
