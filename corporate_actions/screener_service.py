"""Enhanced screener service: unified fundamentals+technical screening logic.

Provides `screen_universe` which returns a paginated, filtered, sorted list
of candidate stock rows using existing `sources` primitives.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Callable
import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
import heapq

from . import sources
from . import config
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor as _TPE
from . import redis_cache
import hashlib
import json
import os


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=8)
def _load_universe_symbols(universe: str) -> tuple[str, ...]:
    try:
        symbols = sources.get_index_universe(universe) or []
    except Exception:
        symbols = []
    if not symbols:
        symbols = (
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
            "LTIM", "SBIN", "ITC", "SUNPHARMA", "AXISBANK",
            "BHARTIARTL", "WIPRO", "KOTAKBANK", "HINDUNILVR", "TATACONSUM",
        )
    return tuple(symbols)


def _build_row(symbol: str) -> dict:
    # fetch fundamentals + quote with retry/backoff to tolerate transient failures
    def fetch_fund():
        return sources.get_fundamentals(symbol, with_screener=True) or {}

    def fetch_quote():
        try:
            return sources.get_best_quote("NSE", symbol) or {}
        except Exception:
            return sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}

    fund = _fetch_with_retry(fetch_fund)
    quote = _fetch_with_retry(fetch_quote)
    try:
        fund = sources.normalise_fundamentals(symbol, dict(fund or {}), quote or {})
    except Exception:
        pass
    price = _safe_float(quote.get("price") if quote.get("price") is not None else fund.get("price"))
    return {
        "symbol": symbol,
        "company": fund.get("company") or fund.get("name") or quote.get("name") or symbol,
        "exchange": "NSE",
        "pe": _safe_float(fund.get("pe")),
        "roe": _safe_float(fund.get("roe")),
        "roce": _safe_float(fund.get("roce")),
        "debt_to_equity": _safe_float(fund.get("debt_to_equity")),
        "div_yield": _safe_float(fund.get("div_yield")),
        "market_cap": _safe_float(fund.get("market_cap") if fund.get("market_cap") is not None else fund.get("mcap_cr")),
        "price": price,
        "change_pct": _safe_float(quote.get("change_pct")),
        "rsi14": _safe_float(fund.get("rsi14") if fund.get("rsi14") is not None else fund.get("rsi")),
        "macd_bull": bool(fund.get("macd_bull")),
        "above_ema200": bool(fund.get("above_ema200") if fund.get("above_ema200") is not None else fund.get("above_sma200")),
        "sector": fund.get("sector") or fund.get("industry") or None,
        "sector_pe": _safe_float(fund.get("sector_pe")),
        "wk52_high": _safe_float(fund.get("wk52_high")),
        "wk52_low": _safe_float(fund.get("wk52_low")),
        "sma_50": _safe_float(fund.get("sma_50")),
        "sma_200": _safe_float(fund.get("sma_200")),
    }


# Simple TTL cache for built rows to avoid hammering data sources repeatedly.
_ROW_CACHE_TTL = int(getattr(config, "SCREENER_ROW_TTL", 60))
_row_cache: dict[str, tuple[float, dict]] = {}


def _get_cached_row(symbol: str) -> dict:
    now = time.time()
    ent = _row_cache.get(symbol)
    if ent and now - ent[0] < _ROW_CACHE_TTL:
        return ent[1]
    # try Redis temporary cache first (best-effort)
    try:
        if redis_cache.is_available():
            key = f"screener:row:{symbol}"
            cached = redis_cache.get(key)
            if cached:
                return cached
    except Exception:
        pass

    row = _build_row(symbol)
    _row_cache[symbol] = (now, row)
    try:
        if redis_cache.is_available():
            redis_cache.set(key, row, ttl=_ROW_CACHE_TTL)
    except Exception:
        pass
    return row


async def _fetch_with_retry_async(func: Callable[[], Any]) -> Any:
    attempts = int(getattr(config, "SCREENER_RETRY_ATTEMPTS", 2))
    backoff = float(getattr(config, "SCREENER_RETRY_BACKOFF", 0.5))
    source_timeout = float(getattr(config, "SCREENER_SOURCE_TIMEOUT", 5.0))
    last_exc = None
    for i in range(attempts + 1):
        try:
            # run the blocking func in a thread with an enforced timeout
            return await asyncio.wait_for(asyncio.to_thread(func), timeout=source_timeout)
        except Exception as e:
            last_exc = e
            # treat timeouts specially so we can retry
            if isinstance(e, asyncio.TimeoutError):
                # allow retrying on timeout
                pass
            if i < attempts:
                await asyncio.sleep(backoff * (2 ** i))
            else:
                logging.getLogger(__name__).warning("fetch_with_retry_async failed after retries: %s", last_exc)
                return {}
    return {}


async def _build_row_async(symbol: str) -> dict:
    def fetch_fund():
        return sources.get_fundamentals(symbol, with_screener=True) or {}

    # prefer an async quote fetch when available to avoid threads
    # Prefer native async path but enforce per-source timeout; fall back to
    # the threaded retry wrapper when async path is missing or times out.
    source_timeout = float(getattr(config, "SCREENER_SOURCE_TIMEOUT", 5.0))
    try:
        if hasattr(sources, "get_fundamentals_async"):
            try:
                fund = await asyncio.wait_for(sources.get_fundamentals_async(symbol, True), timeout=source_timeout)
                if not fund:
                    fund = await _fetch_with_retry_async(fetch_fund)
            except asyncio.TimeoutError:
                fund = await _fetch_with_retry_async(fetch_fund)
            except Exception:
                fund = await _fetch_with_retry_async(fetch_fund)
        else:
            fund = await _fetch_with_retry_async(fetch_fund)
    except Exception:
        fund = {}
    try:
        # call async quote if source provides it, with timeout
        def fetch_quote():
            try:
                if hasattr(sources, "get_best_quote"):
                    return sources.get_best_quote("NSE", symbol) or {}
            except Exception:
                pass
            return sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}

        if hasattr(sources, "get_quote_async"):
            try:
                quote = await asyncio.wait_for(sources.get_quote_async("NSE", symbol), timeout=source_timeout)
                if not quote:
                    quote = await asyncio.wait_for(sources.get_quote_async("BSE", symbol), timeout=source_timeout)
            except asyncio.TimeoutError:
                quote = await _fetch_with_retry_async(fetch_quote)
            except Exception:
                quote = await _fetch_with_retry_async(fetch_quote)
        else:
            quote = await _fetch_with_retry_async(fetch_quote)
    except Exception:
        quote = {}
    try:
        fund = sources.normalise_fundamentals(symbol, dict(fund or {}), quote or {})
    except Exception:
        pass
    price = _safe_float(quote.get("price") if (quote or {}).get("price") is not None else (fund or {}).get("price"))
    fund = fund or {}
    quote = quote or {}
    return {
        "symbol": symbol,
        "company": fund.get("company") or fund.get("name") or quote.get("name") or symbol,
        "exchange": "NSE",
        "pe": _safe_float(fund.get("pe")),
        "roe": _safe_float(fund.get("roe")),
        "roce": _safe_float(fund.get("roce")),
        "debt_to_equity": _safe_float(fund.get("debt_to_equity")),
        "div_yield": _safe_float(fund.get("div_yield")),
        "market_cap": _safe_float(fund.get("market_cap") if fund.get("market_cap") is not None else fund.get("mcap_cr")),
        "price": price,
        "change_pct": _safe_float(quote.get("change_pct")),
        "rsi14": _safe_float(fund.get("rsi14") if fund.get("rsi14") is not None else fund.get("rsi")),
        "macd_bull": bool(fund.get("macd_bull")),
        "above_ema200": bool(fund.get("above_ema200") if fund.get("above_ema200") is not None else fund.get("above_sma200")),
        "sector": fund.get("sector") or fund.get("industry") or None,
        "sector_pe": _safe_float(fund.get("sector_pe")),
        "wk52_high": _safe_float(fund.get("wk52_high")),
        "wk52_low": _safe_float(fund.get("wk52_low")),
        "sma_50": _safe_float(fund.get("sma_50")),
        "sma_200": _safe_float(fund.get("sma_200")),
    }


async def _get_cached_row_async(symbol: str) -> dict:
    now = time.time()
    ent = _row_cache.get(symbol)
    if ent and now - ent[0] < _ROW_CACHE_TTL:
        return ent[1]
    # try Redis temporary cache first (best-effort)
    try:
        if redis_cache.is_available():
            key = f"screener:row:{symbol}"
            cached = redis_cache.get(key)
            if cached:
                return cached
    except Exception:
        pass

    row = await _build_row_async(symbol)
    _row_cache[symbol] = (now, row)
    try:
        if redis_cache.is_available():
            redis_cache.set(key, row, ttl=_ROW_CACHE_TTL)
    except Exception:
        pass
    return row


def _fetch_with_retry(func: Callable[[], Any]) -> Any:
    attempts = int(getattr(config, "SCREENER_RETRY_ATTEMPTS", 2))
    backoff = float(getattr(config, "SCREENER_RETRY_BACKOFF", 0.5))
    source_timeout = float(getattr(config, "SCREENER_SOURCE_TIMEOUT", 5.0))
    last_exc = None
    for i in range(attempts + 1):
        try:
            # run blocking call but bound it using a short-lived ThreadPoolExecutor
            with _TPE(max_workers=1) as ex:
                fut = ex.submit(func)
                return fut.result(timeout=source_timeout)
        except Exception as e:
            last_exc = e
            if i < attempts:
                time.sleep(backoff * (2 ** i))
            else:
                logging.getLogger(__name__).warning("fetch_with_retry failed after retries: %s", last_exc)
                # final attempt failed: return empty-safe value
                return {}
    # fallback
    return {}


def _apply_filters(rows: list[dict], filters: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        pe = _safe_float(r.get("pe"))
        roe = _safe_float(r.get("roe"))
        roce = _safe_float(r.get("roce"))
        debt = _safe_float(r.get("debt_to_equity"))
        div_yield = _safe_float(r.get("div_yield"))
        market_cap = _safe_float(r.get("market_cap"))
        price = _safe_float(r.get("price"))
        rsi = _safe_float(r.get("rsi14"))
        macd = bool(r.get("macd_bull"))
        ema = bool(r.get("above_ema200"))

        if filters.get("pe_min") is not None and (pe is None or pe < float(filters["pe_min"])):
            continue
        if filters.get("pe_max") is not None and (pe is None or pe > float(filters["pe_max"])):
            continue
        if filters.get("roe_min") is not None and (roe is None or roe < float(filters["roe_min"])):
            continue
        if filters.get("roe_max") is not None and (roe is None or roe > float(filters["roe_max"])):
            continue
        if filters.get("roce_min") is not None and (roce is None or roce < float(filters["roce_min"])):
            continue
        if filters.get("div_yield_min") is not None and (div_yield is None or div_yield < float(filters["div_yield_min"])):
            continue
        if filters.get("debt_to_equity_max") is not None and (debt is None or debt > float(filters["debt_to_equity_max"])):
            continue
        if filters.get("market_cap_min") is not None and (market_cap is None or market_cap < float(filters["market_cap_min"])):
            continue
        if filters.get("market_cap_max") is not None and (market_cap is None or market_cap > float(filters["market_cap_max"])):
            continue
        if filters.get("price_min") is not None and (price is None or price < float(filters["price_min"])):
            continue
        if filters.get("price_max") is not None and (price is None or price > float(filters["price_max"])):
            continue

        if filters.get("rsi_min") is not None and (rsi is None or rsi < float(filters["rsi_min"])):
            continue
        if filters.get("rsi_max") is not None and (rsi is None or rsi > float(filters["rsi_max"])):
            continue

        if filters.get("require_macd_bull") and not macd:
            continue
        if filters.get("require_above_ema200") and not ema:
            continue

        if filters.get("change_pct_min") is not None and (r.get("change_pct") is None or float(r.get("change_pct")) < float(filters["change_pct_min"])):
            continue
        if filters.get("change_pct_max") is not None and (r.get("change_pct") is None or float(r.get("change_pct")) > float(filters["change_pct_max"])):
            continue

        if filters.get("exchange") and (r.get("exchange") or "").upper() != str(filters.get("exchange")).upper():
            continue
        sector_filter = (filters.get("sector") or "").strip()
        if sector_filter and sector_filter.lower() not in (r.get("sector") or "").lower():
            continue
        name_contains = filters.get("name_contains")
        if name_contains and name_contains.lower() not in (r.get("company") or "").lower():
            continue
        symbol_contains = filters.get("symbol_contains")
        if symbol_contains and symbol_contains.lower() not in (r.get("symbol") or "").lower():
            continue

        out.append(r)
    return out


def _sort_rows(rows: list[dict], sort_key: str, ascending: bool) -> list[dict]:
    if not rows:
        return rows
    key_map = {
        "symbol": lambda r: (r.get("symbol") or "").upper(),
        "company": lambda r: (r.get("company") or "").upper(),
        "market_cap": lambda r: float(r.get("market_cap") or 0.0),
        "pe": lambda r: float(r.get("pe") or 999999),
        "roe": lambda r: float(r.get("roe") or -999999),
        "roce": lambda r: float(r.get("roce") or -999999),
        "div_yield": lambda r: float(r.get("div_yield") if r.get("div_yield") is not None else -999999),
        "debt_to_equity": lambda r: float(r.get("debt_to_equity") if r.get("debt_to_equity") is not None else 999999),
        "rsi": lambda r: float(r.get("rsi14") or 0.0),
        "rsi14": lambda r: float(r.get("rsi14") or 0.0),
        "change_pct": lambda r: float(r.get("change_pct") or 0.0),
        "price": lambda r: float(r.get("price") or 0.0),
        "sector": lambda r: (r.get("sector") or "").upper(),
    }
    return sorted(rows, key=key_map.get(sort_key, key_map["market_cap"]), reverse=not ascending)


def _get_sort_key_func(sort_key: str):
    key_map = {
        "symbol": lambda r: (r.get("symbol") or "").upper(),
        "company": lambda r: (r.get("company") or "").upper(),
        "market_cap": lambda r: float(r.get("market_cap") or 0.0),
        "pe": lambda r: float(r.get("pe") or 999999),
        "roe": lambda r: float(r.get("roe") or -999999),
        "roce": lambda r: float(r.get("roce") or -999999),
        "div_yield": lambda r: float(r.get("div_yield") if r.get("div_yield") is not None else -999999),
        "debt_to_equity": lambda r: float(r.get("debt_to_equity") if r.get("debt_to_equity") is not None else 999999),
        "rsi": lambda r: float(r.get("rsi14") or 0.0),
        "rsi14": lambda r: float(r.get("rsi14") or 0.0),
        "change_pct": lambda r: float(r.get("change_pct") or 0.0),
        "price": lambda r: float(r.get("price") or 0.0),
        "sector": lambda r: (r.get("sector") or "").upper(),
    }
    return key_map.get(sort_key, key_map["market_cap"])


def screen_universe(
    universe: str = "nifty500",
    filters: dict[str, Any] | None = None,
    sort: str = "market_cap",
    ascending: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    filters = filters or {}
    symbols = list(_load_universe_symbols(universe))

    # determine how many top rows we need to keep in memory
    top_k = max(0, offset) + max(0, limit)
    if top_k <= 0:
        return []

    keyfunc = _get_sort_key_func(sort)
    max_workers = int(getattr(config, "SCREENER_MAX_WORKERS", 12))
    max_workers = max(1, max_workers)

    def gen_rows():
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_get_cached_row, symbol): symbol for symbol in symbols}
            for fut in as_completed(futures):
                try:
                    row = fut.result()
                    if row:
                        yield row
                except Exception:
                    continue

    # use heapq's nlargest/nsmallest which will iterate the generator
    if ascending:
        selected = heapq.nsmallest(top_k, gen_rows(), key=keyfunc)
    else:
        selected = heapq.nlargest(top_k, gen_rows(), key=keyfunc)

    filtered = _apply_filters(selected, filters)
    final_sorted = _sort_rows(filtered, sort, ascending)
    start = max(0, offset)
    end = start + max(0, limit)
    return final_sorted[start:end]


async def screen_universe_async(
    universe: str = "nifty500",
    filters: dict[str, Any] | None = None,
    sort: str = "market_cap",
    ascending: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    filters = filters or {}
    symbols = list(_load_universe_symbols(universe))

    top_k = max(0, offset) + max(0, limit)
    if top_k <= 0:
        return []

    keyfunc = _get_sort_key_func(sort)
    sem = asyncio.Semaphore(int(getattr(config, "SCREENER_MAX_WORKERS", 12)))

    # overall deadline to bound the async screener run and return partial
    # results when the endpoint-level timeout is reached.
    api_timeout = float(os.getenv("SCREENER_API_TIMEOUT", "15"))
    deadline = time.time() + max(1.0, api_timeout)

    # prefer cached rows first so a warm server can return fast partial
    # results without scanning the entire universe.
    cached_symbols = [s for s in symbols if s in _row_cache]
    remaining = [s for s in symbols if s not in _row_cache]
    ordered_symbols = cached_symbols + remaining

    async def worker(sym: str):
        async with sem:
            try:
                r = await _get_cached_row_async(sym)
                return r
            except Exception:
                return None

    tasks = [asyncio.create_task(worker(s)) for s in ordered_symbols]

    # wait until every task finishes or the deadline expires, then cancel the
    # stragglers so this coroutine always returns promptly with what it has.
    results: list[dict] = []
    pending = set(tasks)
    while pending:
        remaining_seconds = deadline - time.time()
        if remaining_seconds <= 0:
            break
        done, pending = await asyncio.wait(pending, timeout=remaining_seconds)
        for task in done:
            row = task.result()
            if row:
                results.append(row)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if ascending:
        selected = heapq.nsmallest(top_k, results, key=keyfunc)
    else:
        selected = heapq.nlargest(top_k, results, key=keyfunc)

    filtered = _apply_filters(selected, filters)
    final_sorted = _sort_rows(filtered, sort, ascending)
    start = max(0, offset)
    end = start + max(0, limit)
    return final_sorted[start:end]


async def prewarm_universe(universe: str = "nifty500", limit: int = 100) -> None:
    """Background cache pre-warm: build cached rows for the top `limit` symbols.

    This runs best-effort and swallows errors; it is intended to be scheduled
    on application startup so the first UI requests are faster.
    """
    symbols = list(_load_universe_symbols(universe))[: max(0, int(limit))]
    if not symbols:
        return
    sem = asyncio.Semaphore(int(getattr(config, "SCREENER_MAX_WORKERS", 6)))
    # bound the prewarm so a slow source cannot block server startup forever
    prewarm_deadline = time.time() + float(getattr(config, "SCREENER_SOURCE_TIMEOUT", 5.0)) * 2

    async def worker(sym: str):
        if time.time() > prewarm_deadline:
            return
        async with sem:
            try:
                await asyncio.wait_for(
                    _get_cached_row_async(sym),
                    timeout=max(0.5, prewarm_deadline - time.time()),
                )
            except Exception:
                pass

    tasks = [asyncio.create_task(worker(s)) for s in symbols]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        pass
