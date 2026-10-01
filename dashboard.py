"""Custom web dashboard for the Stock Alert Bot.

This is the modern, non-Streamlit app shell. It serves a real HTML dashboard
and exposes the core data endpoints used by the UI.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import logging
import traceback

from corporate_actions import admin as admin_service
from corporate_actions import sources, storage
from corporate_actions import snapshots as snapshots_service
from corporate_actions.logging_setup import setup_logging
from corporate_actions.screener_service import screen_universe_async
from corporate_actions.market import hours as market_hours
from corporate_actions.telegram import client as telegram_client
import asyncio
import threading

setup_logging()
log = logging.getLogger(__name__)

# Fields the source layer fabricates even when nothing was found (identity
# placeholders + derived booleans). A fund merge containing ONLY these is a
# stub => the symbol almost certainly does not exist.
_FUND_STUB_KEYS = {"company", "name", "macd_bull", "above_ema200", "above_sma200", "data_sources"}


def _is_stub_fund(fund: dict) -> bool:
    """True when a fundamentals fetch found nothing substantive."""
    return not any(
        key not in _FUND_STUB_KEYS and value not in (None, "", [], {})
        for key, value in (fund or {}).items()
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Royal Stock", version="2.0.0")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# logging is configured centrally by setup_logging() above; the level can
# still be tuned per environment with the LOG_LEVEL environment variable.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# middleware to catch and log unexpected exceptions (including tracebacks)
@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    log = logging.getLogger(__name__)
    try:
        return await call_next(request)
    except asyncio.CancelledError:
        # keep cancelled errors propagating after logging
        log.warning("Request cancelled: %s %s", request.method, request.url)
        raise
    except Exception as exc:
        log.exception("Unhandled exception processing request %s %s: %s", request.method, request.url, exc)
        # return a safe JSON 500 so clients don't hang waiting
        return JSONResponse({"detail": "internal server error"}, status_code=500)


def _exception_handler(request: Request, exc: Exception):
    log = logging.getLogger(__name__)
    log.exception("Global exception handler caught: %s", exc)
    return JSONResponse({"detail": "internal server error"}, status_code=500)


app.add_exception_handler(Exception, _exception_handler)


@app.on_event("startup")
async def _startup_prewarm():
    try:
        from corporate_actions import screener_service
        # schedule prewarm in background with a smaller footprint to avoid
        # saturating outgoing connections on startup
        asyncio.create_task(screener_service.prewarm_universe("nifty500", limit=20))
    except Exception as error:
        log.warning("_exception_handler: %s", error)
        pass


def _fallback_index_html() -> str:
    return """<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>Royal Stock</title>
    <style>
      body { font-family: Arial, sans-serif; background: #f3f7fb; color: #0f172a; margin: 0; padding: 24px; }
      .wrap { max-width: 960px; margin: 0 auto; background: #fff; border: 1px solid #dfe7f0; border-radius: 18px; padding: 28px; box-shadow: 0 16px 32px rgba(15, 23, 42, 0.08); }
      h1 { margin-top: 0; }
      .muted { color: #475569; }
      .code { background: #eef5ff; border: 1px solid #d9e7ff; border-radius: 10px; padding: 10px 12px; display: inline-block; }
      nav a { margin-right: 12px; }
    </style>
  </head>
  <body>
    <div class=\"wrap\">
    <h1>📈 Royal Stock</h1>
      <p class=\"muted\">Custom dashboard · no Streamlit shell</p>
      <p>The dashboard is running, but the template renderer could not load the HTML shell.</p>
      <nav>
        <a href=\"/\">Dashboard</a>
        <a href=\"/watchlist\">Watchlist</a>
        <a href=\"/fundamentals\">Fundamentals</a>
        <a href=\"/market\">Screener</a>
        <a href=\"/exdates\">Corporate actions</a>
        <a href=\"/system\">System</a>
      </nav>
      <div class=\"code\">/api/screener</div>
      <p class=\"muted\">Refresh in a moment or check the app logs for template issues.</p>
    </div>
  </body>
</html>"""


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


def _candidate_rows_from_universe(universe: str, limit: int = 200) -> list[dict]:
    try:
        symbols = sources.get_index_universe(universe)
    except Exception:
        symbols = []
    if not symbols:
        symbols = [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "LTIM",
            "SBIN", "ITC", "SUNPHARMA", "AXISBANK", "BHARTIARTL",
            "WIPRO", "KOTAKBANK", "HINDUNILVR", "TATACONSUM",
        ]

    rows: list[dict] = []
    for symbol in symbols[:limit]:
        try:
            fund = sources.get_fundamentals(symbol, with_screener=True) or {}
            quote = sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}
            row = {
                "symbol": symbol,
                "company": fund.get("company") or fund.get("name") or symbol,
                "exchange": "NSE",
                "pe": _safe_float(fund.get("pe")),
                "roe": _safe_float(fund.get("roe")),
                "debt_to_equity": _safe_float(fund.get("debt_to_equity")),
                "market_cap": _safe_float(fund.get("market_cap")),
                "price": _safe_float(quote.get("price")),
                "change_pct": _safe_float(quote.get("change_pct")),
                "rsi14": _safe_float(fund.get("rsi14")),
                "macd_bull": bool(fund.get("macd_bull")),
                "above_ema200": bool(fund.get("above_ema200")),
            }
            if row["symbol"]:
                rows.append(row)
        except Exception as error:
            log.warning("_candidate_rows_from_universe: %s", error)
            continue
    return rows


def _filter_rows(rows: list[dict], filters: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        pe = _safe_float(row.get("pe"))
        roe = _safe_float(row.get("roe"))
        debt = _safe_float(row.get("debt_to_equity"))
        market_cap = _safe_float(row.get("market_cap"))
        rsi = _safe_float(row.get("rsi14"))
        macd_bull = bool(row.get("macd_bull"))
        above_ema200 = bool(row.get("above_ema200"))

        if filters.get("pe_max") is not None and (pe is None or pe > float(filters["pe_max"])):
            continue
        if filters.get("roe_min") is not None and (roe is None or roe < float(filters["roe_min"])):
            continue
        if filters.get("debt_to_equity_max") is not None and (debt is None or debt > float(filters["debt_to_equity_max"])):
            continue
        if filters.get("market_cap_min") is not None and (market_cap is None or market_cap < float(filters["market_cap_min"])):
            continue
        if filters.get("rsi_min") is not None and (rsi is None or rsi < float(filters["rsi_min"])):
            continue
        if filters.get("rsi_max") is not None and (rsi is None or rsi > float(filters["rsi_max"])):
            continue
        if filters.get("require_macd_bull") and not macd_bull:
            continue
        if filters.get("require_above_ema200") and not above_ema200:
            continue
        out.append(row)
    return out


def _sort_rows(rows: list[dict], sort_key: str, ascending: bool) -> list[dict]:
    if not rows:
        return rows
    key_map = {
        "symbol": lambda r: (r.get("symbol") or "").upper(),
        "market_cap": lambda r: float(r.get("market_cap") or 0.0),
        "pe": lambda r: float(r.get("pe") or 999999),
        "roe": lambda r: float(r.get("roe") or -999999),
        "rsi": lambda r: float(r.get("rsi14") or 0.0),
        "change_pct": lambda r: float(r.get("change_pct") or 0.0),
        "price": lambda r: float(r.get("price") or 0.0),
    }
    return sorted(rows, key=key_map.get(sort_key, key_map["market_cap"]), reverse=not ascending)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def read_index(request: Request):
    try:
        return templates.TemplateResponse(request, "index.html")
    except Exception as exc:
        # log the template rendering error for debugging
        log = logging.getLogger(__name__)
        log.error("Template render failed: %s", exc)
        log.debug(traceback.format_exc())
        # serve the self-contained fallback page (raw template source would
        # just show Jinja tags to the browser since pages extend base.html)
        return HTMLResponse(_fallback_index_html())


@app.get("/api/watchlist")
async def api_watchlist(enrich: bool = Query(False)):
    items = storage.load_watchlist()
    if not enrich:
        return JSONResponse(items)
    # Enriched mode: attach live price/change so the Watchlist tab shows
    # live values without N extra round-trips from the browser.
    enriched: list[dict] = []
    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        exchange = str(item.get("exchange") or "NSE").upper()
        row = dict(item)
        try:
            quote = await asyncio.to_thread(sources.get_best_quote, exchange, symbol)
        except Exception:
            quote = None
        try:
            if not quote:
                quote = await asyncio.to_thread(
                    lambda s=symbol, e=exchange: sources.get_quote(e, s)
                    or sources.get_quote("BSE" if e == "NSE" else "NSE", s) or {}
                )
        except Exception:
            quote = quote or {}
        quote = quote or {}
        row["price"] = quote.get("price")
        row["change_pct"] = quote.get("change_pct")
        row["prev_close"] = quote.get("prev_close")
        row["quote_source"] = quote.get("source") or "none"
        enriched.append(row)
    return JSONResponse(enriched)


async def _validate_symbol(symbol: str, market: str) -> dict:
    """Resolve a symbol against real data sources before any user action.

    Ladder — IN: Yahoo quote (NSE suffix) -> BSE suffix -> the NSE master
    stock list; US: Yahoo quote (bare ticker) -> Yahoo ticker search.
    Only a resolvable price or an exact hit in the exchange master list
    counts as valid, so typos and delisted names are rejected.
    """
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO").removesuffix(".US")
    want_us = market.strip().lower() in ("us", "usa", "nasdaq", "nyse")

    def _check_in() -> dict:
        if sources.get_quote("NSE", key):
            return {"symbol": key, "valid": True, "exchange": "NSE", "source": "yahoo"}
        if sources.get_quote("BSE", key):
            return {"symbol": key, "valid": True, "exchange": "BSE", "source": "yahoo-bse"}
        for hit in sources.search_stocks(key, limit=3):
            if str(hit.get("symbol") or "").upper() == key:
                return {"symbol": key, "valid": True, "exchange": "NSE", "source": "nse-list"}
        return {"symbol": key, "valid": False, "reason": "not-found"}

    def _check_us() -> dict:
        if sources.get_quote("US", key):
            return {"symbol": key, "valid": True, "exchange": "US", "source": "yahoo"}
        for hit in sources.search_us_tickers(key, limit=5):
            if str(hit.get("symbol") or "").upper() == key:
                return {"symbol": key, "valid": True, "exchange": "US", "source": "yahoo-search"}
        return {"symbol": key, "valid": False, "reason": "not-found"}

    try:
        return await asyncio.to_thread(_check_us if want_us else _check_in)
    except Exception as exc:
        # Never block the user on a validator outage — fail open, marked degraded.
        log.warning("symbol validation degraded for %s: %s", key, exc)
        return {"symbol": key, "valid": True, "degraded": True, "reason": str(exc)}


@app.get("/api/validate")
async def api_validate(symbol: str | None = Query(None), market: str = Query("in")):
    """Check a symbol actually resolves to a real, tradable stock.

    Used by the UI before offering actions (watchlist add, refresh, CSV)
    so an invalid or delisted symbol can never be acted on.
    """
    if not symbol or not symbol.strip():
        return JSONResponse({"symbol": "", "valid": False, "reason": "empty"})
    return JSONResponse(await _validate_symbol(symbol, market or "in"))


async def _resolve_watchlist_items(items: list) -> tuple[list[dict], list[dict]]:
    """Validate watchlist candidates; unknown symbols are rejected, never stored.

    Market detection: an explicit item market wins; otherwise try IN first,
    then fall back to US so a single pasted list can mix both.
    """
    validated: list[dict] = []
    rejected: list[dict] = []
    seen: set = set()
    for item in items:
        item = item or {}
        sym = str(item.get("symbol") or "").strip().upper().removesuffix(".NS").removesuffix(".BO").removesuffix(".US")
        if not sym:
            rejected.append({"symbol": "", "reason": "empty"})
            continue
        key = ((item.get("exchange") or "").upper(), sym)
        if key in seen:
            continue
        explicit_market = str(item.get("market") or "").strip().lower()
        check: dict = {}
        if explicit_market in ("in", "us"):
            check = await _validate_symbol(sym, explicit_market)
            if not check.get("valid"):
                rejected.append({"symbol": sym, "reason": check.get("reason") or "not-found"})
                continue
        else:
            check = await _validate_symbol(sym, "in")
            if not check.get("valid"):
                check = await _validate_symbol(sym, "us")
            if not check.get("valid"):
                rejected.append({"symbol": sym, "reason": check.get("reason") or "not-found"})
                continue
        seen.add(key)
        validated.append({
            "symbol": sym,
            "company": str(item.get("company") or ""),
            "exchange": (check.get("exchange") or item.get("exchange") or "NSE").upper(),
        })
    return validated, rejected


@app.post("/api/watchlist")
async def add_watchlist(payload: dict):
    """Bulk watchlist write.

    Body: {"symbols": ["A","B"]} or {"items": [{symbol, company, market}...]}.
    mode "add" (default) adds missing symbols only; mode "exact" replaces the
    whole list with exactly the given symbols (duplicates collapse, order kept).
    """
    mode = str(payload.get("mode") or "add").strip().lower()
    items = payload.get("items")
    if items is None:
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise HTTPException(status_code=400, detail="provide 'symbols' (list) or 'items' (list)")
        items = [{"symbol": s} for s in symbols]
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")
    validated, rejected = await _resolve_watchlist_items(items)
    if rejected:
        log.info("watchlist %s rejected invalid symbols: %s", mode, [r["symbol"] for r in rejected])
    if not validated and rejected:
        raise HTTPException(status_code=422, detail="Unknown symbol(s): " + ", ".join(r["symbol"] or "(empty)" for r in rejected))
    if mode == "exact":
        if rejected:
            # All-or-nothing: a typo in a 50-symbol list must never silently
            # wipe the valid stocks. Same rule the Telegram /setlist applies.
            raise HTTPException(
                status_code=422,
                detail="Nothing was changed - unknown symbol(s): "
                + ", ".join(r["symbol"] or "(empty)" for r in rejected),
            )
        result = storage.replace_watchlist(validated)
        before = {str(i.get("symbol", "")).upper() for i in storage.load_watchlist()}
        return JSONResponse({
            "items": result["list"],
            "count": result["added"],
            "added": result["added"],
            "skipped_duplicates": result["skipped_duplicates"],
            "kept": sorted({str(i.get("symbol", "")).upper() for i in result["list"]} & before),
            "rejected": rejected,
            "mode": "exact",
        })
    result = storage.add_to_watchlist(validated)
    return JSONResponse({
        "items": result,
        "count": len(result),
        "added": len(validated),
        "rejected": rejected,
        "mode": "add",
    })


@app.delete("/api/watchlist")
async def delete_watchlist(symbol: str | None = Query(None), exchange: str | None = Query("NSE")):
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    try:
        remaining = storage.remove_from_watchlist(symbol, exchange)
        return JSONResponse({"items": remaining, "count": len(remaining)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/watchlist.csv")
async def api_watchlist_csv():
    items = storage.load_watchlist()
    import csv, io
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["symbol", "company"])
    for it in items:
        w.writerow([it.get("symbol"), it.get("company")])
    return HTMLResponse(content=si.getvalue(), media_type="text/csv")


@app.get("/api/screener")
async def api_screener(
    universe: str = Query("nifty500"),
    pe_min: float | None = Query(None),
    pe_max: float | None = Query(None),
    roe_min: float | None = Query(None),
    roe_max: float | None = Query(None),
    roce_min: float | None = Query(None),
    div_yield_min: float | None = Query(None),
    debt_max: float | None = Query(None),
    market_cap_min: float | None = Query(None),
    market_cap_max: float | None = Query(None),
    price_min: float | None = Query(None),
    price_max: float | None = Query(None),
    rsi_min: float | None = Query(None),
    rsi_max: float | None = Query(None),
    require_macd_bull: bool = Query(False),
    require_above_ema200: bool = Query(False),
    change_pct_min: float | None = Query(None),
    change_pct_max: float | None = Query(None),
    exchange: str | None = Query(None),
    sector: str | None = Query(None),
    name_contains: str | None = Query(None),
    symbol_contains: str | None = Query(None),
    sort: str = Query("market_cap"),
    ascending: bool = Query(False),
    limit: int = Query(25, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    filters = {
        "pe_min": pe_min,
        "pe_max": pe_max,
        "roe_min": roe_min,
        "roe_max": roe_max,
        "roce_min": roce_min,
        "div_yield_min": div_yield_min,
        "debt_to_equity_max": debt_max,
        "market_cap_min": market_cap_min,
        "market_cap_max": market_cap_max,
        "price_min": price_min,
        "price_max": price_max,
        "rsi_min": rsi_min,
        "rsi_max": rsi_max,
        "require_macd_bull": require_macd_bull,
        "require_above_ema200": require_above_ema200,
        "change_pct_min": change_pct_min,
        "change_pct_max": change_pct_max,
        "exchange": exchange,
        "sector": sector,
        "name_contains": name_contains,
        "symbol_contains": symbol_contains,
    }
    log = logging.getLogger(__name__)
    timeout = float(os.getenv("SCREENER_API_TIMEOUT", "15"))
    # give the service a small grace window past the endpoint timeout so it can
    # return its best partial results instead of being cancelled at the deadline
    service_timeout = timeout + 1.5
    try:
        rows = await asyncio.wait_for(
            screen_universe_async(universe=universe, filters=filters, sort=sort, ascending=ascending, limit=limit, offset=offset),
            timeout=service_timeout,
        )
        # Preserve what this page view returned, timestamped (best-effort).
        storage.capture_command_output(
            "screener", "GET /api/screener", rows, prefix="view_",
            universe=universe, sort=sort, ascending=ascending,
            limit=limit, offset=offset, filters=filters,
        )
        return JSONResponse(rows)
    except asyncio.TimeoutError:
        log.warning("/api/screener timed out after %s seconds", service_timeout)
        raise HTTPException(status_code=504, detail="screener timeout")
    except Exception as e:
        log.exception("/api/screener failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/screener.csv")
async def api_screener_csv(
    universe: str = Query("nifty500"),
    pe_min: float | None = Query(None),
    pe_max: float | None = Query(None),
    roe_min: float | None = Query(None),
    roe_max: float | None = Query(None),
    roce_min: float | None = Query(None),
    div_yield_min: float | None = Query(None),
    debt_max: float | None = Query(None),
    market_cap_min: float | None = Query(None),
    market_cap_max: float | None = Query(None),
    price_min: float | None = Query(None),
    price_max: float | None = Query(None),
    rsi_min: float | None = Query(None),
    rsi_max: float | None = Query(None),
    require_macd_bull: bool = Query(False),
    require_above_ema200: bool = Query(False),
    change_pct_min: float | None = Query(None),
    change_pct_max: float | None = Query(None),
    exchange: str | None = Query(None),
    sector: str | None = Query(None),
    name_contains: str | None = Query(None),
    symbol_contains: str | None = Query(None),
    sort: str = Query("market_cap"),
    ascending: bool = Query(False),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    filters = {
        "pe_min": pe_min,
        "pe_max": pe_max,
        "roe_min": roe_min,
        "roe_max": roe_max,
        "roce_min": roce_min,
        "div_yield_min": div_yield_min,
        "debt_to_equity_max": debt_max,
        "market_cap_min": market_cap_min,
        "market_cap_max": market_cap_max,
        "price_min": price_min,
        "price_max": price_max,
        "rsi_min": rsi_min,
        "rsi_max": rsi_max,
        "require_macd_bull": require_macd_bull,
        "require_above_ema200": require_above_ema200,
        "change_pct_min": change_pct_min,
        "change_pct_max": change_pct_max,
        "exchange": exchange,
        "sector": sector,
        "name_contains": name_contains,
        "symbol_contains": symbol_contains,
    }
    log = logging.getLogger(__name__)
    timeout = float(os.getenv("SCREENER_API_TIMEOUT", "30"))
    # same grace window as /api/screener so partial results still arrive
    service_timeout = timeout + 1.5
    try:
        rows = await asyncio.wait_for(
            screen_universe_async(universe=universe, filters=filters, sort=sort, ascending=ascending, limit=limit, offset=offset),
            timeout=service_timeout,
        )
    except asyncio.TimeoutError:
        log.warning("/api/screener.csv timed out after %s seconds", service_timeout)
        raise HTTPException(status_code=504, detail="screener csv timeout")
    except Exception as e:
        log.exception("/api/screener.csv failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    import csv
    import io

    cols = ["symbol", "company", "sector", "price", "change_pct", "pe", "roe", "roce",
            "debt_to_equity", "div_yield", "market_cap", "sector_pe", "rsi14",
            "macd_bull", "above_ema200", "wk52_high", "wk52_low"]

    def gen():
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        yield out.getvalue()
        out.seek(0)
        out.truncate(0)
        for r in rows:
            w.writerow([r.get(c) for c in cols])
            yield out.getvalue()
            out.seek(0)
            out.truncate(0)

    return StreamingResponse(gen(), media_type="text/csv")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    # HEAD must be accepted: uptime monitors (UptimeRobot's default HTTP
    # check) probe with HEAD, and a 405 here reads as the site being down
    # even while it is perfectly healthy.
    return JSONResponse({"status": "ok"})


@app.get("/api/quote")
async def api_quote(symbol: str | None = Query(None), exchange: str = Query("NSE")):
    """Best-effort live quote from free sources (Yahoo -> NSE -> Stooq).

    Always returns 200 with a `source` field ('yahoo'|'nse'|'stooq'|'none')
    so the UI can badge where the price came from instead of blanking.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    ex = (exchange or "NSE").strip().upper()
    try:
        quote = await asyncio.to_thread(sources.get_best_quote, ex, key)
        return JSONResponse({"symbol": key, "exchange": ex, "quote": quote or {"source": "none"}})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/history")
async def api_history(
    symbol: str | None = Query(None),
    exchange: str = Query("NSE"),
    timeframe: str = Query(""),
    range: str = Query(""),
):
    """OHLCV history for the customizable price chart (Yahoo -> Stooq fallback).

    Range mode (chart toolbar): range=1d|5d|1mo|6mo|1y|5y|max — e.g. 1mo is
    one month of hourly bars (not yearly monthly candles). Timeframe mode
    (scanner timeframes): timeframe=5m..1mo. Returns {'symbol','range',
    'timeframe','bars':{timestamp,open,high,low,close,volume,...},'source',
    'downsampled'} with at most ~260 bars, downsampled server-side so the
    canvas chart stays fast.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    ex = (exchange or "NSE").strip().upper()
    range_key = (range or "").strip().lower()
    tf = (timeframe or "").strip().lower()
    if range_key not in ("1d", "5d", "1mo", "6mo", "1y", "5y", "max"):
        range_key = ""
    if not range_key and tf not in ("5m", "15m", "30m", "1h", "4h", "1d", "1w", "1mo"):
        tf = "1d"
    try:
        bars = None
        source = "yahoo"
        used_range = range_key
        if range_key:
            bars = await asyncio.to_thread(sources.get_chart_ohlc, ex, key, range_key)
        else:
            bars = await asyncio.to_thread(sources.get_ohlc, ex, key, tf)
        fallback_used = ""
        if not bars or not bars.get("close"):
            # Degrade instead of blanking: the chart must always show
            # something. Exact range miss -> nearest scanner timeframe ->
            # successively wider chart ranges -> stooq daily CSV (no
            # intraday). The response names the range actually served so
            # the UI can tell the user rather than silently mislead.
            ladder = {
                "1d": "5m", "5d": "15m", "1mo": "1h", "6mo": "1d",
                "1y": "1d", "5y": "1w", "max": "1mo",
            }
            fallback_tf = ladder.get(range_key) or tf or "1d"
            if fallback_tf != "1d" and range_key:
                bars = await asyncio.to_thread(sources.get_ohlc, ex, key, fallback_tf)
                if bars and bars.get("close"):
                    fallback_used = range_key
            if not bars or not bars.get("close"):
                wider = {"1d": "5d", "5d": "1mo", "1mo": "6mo", "6mo": "1y", "1y": "5y", "5y": "max"}
                probe = wider.get(range_key)
                while probe and (not bars or not bars.get("close")):
                    bars = await asyncio.to_thread(sources.get_chart_ohlc, ex, key, probe)
                    if bars and bars.get("close"):
                        fallback_used = probe
                        used_range = probe
                        break
                    probe = wider.get(probe)
            if (not bars or not bars.get("close")) and (range_key in ("", "6mo", "1y", "5y", "max") or tf == "1d"):
                try:
                    bars = await asyncio.to_thread(sources.get_stooq_history, key, ex)
                    source = "stooq"
                except Exception:
                    bars = None
            if not bars or not bars.get("close"):
                return JSONResponse({"symbol": key, "range": range_key or None, "timeframe": tf or None, "bars": None, "source": "none"})
        # Downsample to <= 260 points so the canvas chart stays fast.
        closes = bars.get("close") or []
        downsampled = len(closes) > 260
        if downsampled:
            step = max(1, -(-len(closes) // 260))  # ceil so we always land <= 260
            for field in ("timestamp", "open", "high", "low", "close", "volume"):
                values = bars.get(field) or []
                bars[field] = values[::step]
        bars["source"] = bars.get("source") or source
        return JSONResponse({
            "symbol": key,
            "range": used_range or None,
            "requested_range": range_key or None,
            "fallback": fallback_used or None,
            "timeframe": tf or None,
            "bars": bars,
            "source": bars.get("source"),
            "downsampled": downsampled,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/news")
async def api_news(symbol: str | None = Query(None), limit: int = Query(5, ge=1, le=20)):
    """Latest headlines for a symbol (Google News RSS -> Yahoo fallback)."""
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    try:
        items = await asyncio.to_thread(sources.get_stock_news, "NSE", key, limit)
        return JSONResponse({"symbol": key, "news": items or []})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/search")
async def api_search(q: str | None = Query(None), market: str = Query("in"), limit: int = Query(8, ge=1, le=20)):
    """Symbol autocomplete backed by NSE search + Yahoo provider fallback."""
    term = (q or "").strip()
    if not term:
        return JSONResponse({"results": []})
    try:
        results = await asyncio.to_thread(
            sources.search_market_data, term, {"market": market, "limit": limit}, limit
        )
        return JSONResponse({"results": results or []})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/universe")
async def api_universe(universe: str = Query("nifty500")):
    """Index constituent symbols.

    Universes: NIFTY 100 / NIFTY 500 / NIFTY 1000 (Total Market, ~750 stocks)
    / all NSE equities (``all``, ~2.5k) / NASDAQ 100 / S&P 500.
    """
    try:
        symbols = await asyncio.to_thread(sources.get_index_universe, universe)
        return JSONResponse({"universe": universe, "count": len(symbols or []), "symbols": symbols or []})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/analysis")
async def api_analysis(symbol: str | None = Query(None), market: str = Query("in")):
    """Snapshot ratings, verdict, concerns, positives and the main question.

    Same rules as the Telegram deep report's snapshot/verdict (bot parity),
    returned as plain JSON data for the web Snapshot & Verdict card.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    want_us = (market or "in").strip().lower() in ("us", "usa", "nasdaq", "nyse")
    try:
        from corporate_actions.analysis_service import build_analysis

        if want_us:
            fund = await asyncio.to_thread(sources.get_us_fundamentals, key) or {}
            quote = await asyncio.to_thread(lambda: sources.get_quote("US", key) or {}) or {}
        else:
            fund = await asyncio.to_thread(sources.get_fundamentals, key, True) or {}
            try:
                quote = await asyncio.to_thread(sources.get_best_quote, "NSE", key) or {}
            except Exception:
                quote = await asyncio.to_thread(
                    lambda: sources.get_quote("NSE", key) or sources.get_quote("BSE", key) or {}
                ) or {}
            try:
                fund = sources.normalise_fundamentals(key, dict(fund or {}), quote or {})
            except Exception as error:
                log.warning("gen: %s", error)
                pass
        price = (quote or {}).get("price", (fund or {}).get("price"))
        data = await asyncio.to_thread(build_analysis, fund or {}, price)
        return JSONResponse({"symbol": key, "market": "us" if want_us else "in", **data})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/movers")
async def api_movers(
    mode: str = Query("gainers"),
    universe: str = Query("nifty500"),
    period: str = Query("today"),
    limit: int = Query(20, ge=1, le=100),
    date: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    """Market screens (bot /topmovers /topgainers /toplosers /gappers parity).

    mode: gainers | losers | movers (both, ranked by |move|) | gappers.
    period: 5m 15m 30m 1h 2h 4h today 1d 2d 5d 1w 2w 1mo 3mo 6mo 1y.
    date=YYYY-MM-DD screens ONE historical session (its full-day move / its
    opening gap); date_from + date_to screen a whole range (best single-day
    move per stock inside the range). Top rows are enriched with the
    screener row (pe/roe/mcap/rsi/...) so the table never shows bare
    numbers without context.
    """
    from corporate_actions import screener_service
    from corporate_actions.market import MOVERS_PERIODS

    log = logging.getLogger(__name__)
    mode = (mode or "gainers").strip().lower()
    if mode not in ("gainers", "losers", "movers", "gappers"):
        raise HTTPException(status_code=400, detail="mode must be gainers|losers|movers|gappers")

    # ---- historical date support (bot /toplosers 12-08-2026 parity) ----
    import datetime as _dt
    from corporate_actions.core.dates import parse_date_token

    def _clean_date(value: str):
        value = (value or "").strip()
        if not value:
            return None
        parsed = parse_date_token(value)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"bad date {value!r} (use YYYY-MM-DD)")
        return parsed

    target_date = _clean_date(date)
    range_from = _clean_date(date_from)
    range_to = _clean_date(date_to)
    if range_from and range_to and range_to < range_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    today = _dt.date.today()
    for named, d in (("date", target_date), ("date_to", range_to)):
        if d and d > today:
            raise HTTPException(status_code=400, detail=f"{named} is in the future")
    if target_date and (range_from or range_to):
        raise HTTPException(status_code=400, detail="use either date= or date_from/date_to, not both")

    period_key = (period or "today").strip().lower()
    period_tuple = MOVERS_PERIODS.get(period_key)
    if period_tuple is None and mode != "gappers" and not (target_date or range_from):
        raise HTTPException(
            status_code=400,
            detail=f"unknown period {period_key!r} (try today, 1h, 1d, 1w, 1mo)",
        )
    try:
        symbols = await asyncio.to_thread(sources.get_index_universe, universe) or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not symbols:
        return JSONResponse({"mode": mode, "universe": universe, "period": period_key, "rows": []})
    exchange = sources.universe_exchange(universe)
    # Historical scans fetch a wider chart window per symbol, and one
    # transient Yahoo miss must not silently blank the whole screen — give
    # 1y windows (and range scans, which hit the deadline twice) more room.
    days_span = period_tuple[1] if (period_tuple and period_tuple[0] == "days") else 0
    if target_date:
        days_span = max(1, (today - target_date).days)
    elif range_from:
        days_span = max(1, (today - range_from).days)
    deadline = float(os.getenv("SCREENER_API_TIMEOUT", "15")) + 1.5
    # Date screens are slower (per-day history) and a range scan can hit the
    # deadline twice - give them real room so they don't come back half-empty.
    if days_span > 300:
        deadline += 20
    if target_date:
        deadline += 10
    if range_from:
        deadline += 30
    # Scale with the universe so the broad NSE scans are not cut off after the
    # first ~200 symbols (capped to keep the API responsive).
    try:
        deadline += min(75.0, len(symbols) / 50.0)
    except Exception as error:
        log.warning("_clean_date: %s", error)
        pass
    started = asyncio.get_event_loop().time()

    def _scan_one(symbol: str, _retries: int = 2) -> dict | None:
        from corporate_actions.market import fetch_period_change

        try:
            if mode == "gappers" and not (target_date or range_from):
                move = sources.get_gap_change(exchange, symbol)
                if not move:
                    return None
                return {
                    "symbol": symbol,
                    "price": move.get("price"),
                    "gap_pct": move.get("gap_pct"),
                    "move_from_open_pct": move.get("move_from_open_pct"),
                    "change_pct": move.get("gap_pct"),
                    "name": move.get("name") or symbol,
                }
            if target_date or range_from:
                # Historical screen: best single-session move per stock. A
                # range reuses ONE gap-history fetch per symbol (every
                # session's open/close/prev_close/gap) instead of one fetch
                # per day, which made week-long scans take minutes.
                if range_from:
                    from corporate_actions.sources import get_gap_history

                    history = get_gap_history(
                        exchange, symbol,
                        days=max(3, (range_to - range_from).days + 2),
                    ) or []
                    lo_iso, hi_iso = range_from.isoformat(), range_to.isoformat()
                    best, best_mag = None, 0.0
                    for row in history:
                        day_iso = str(row.get("date") or "")
                        if day_iso < lo_iso or day_iso > hi_iso:
                            continue
                        if mode == "gappers":
                            pct = row.get("gap_pct")
                        else:
                            close, prev = row.get("close"), row.get("prev_close")
                            pct = ((close / prev) - 1.0) * 100.0 if (close and prev) else None
                        if pct is None:
                            continue
                        mag = abs(pct)
                        if best is None or mag > best_mag:
                            best_mag = mag
                            best = {"price": row.get("close"), "change_pct": pct, "date": day_iso}
                            if mag > 15 and mode != "movers":
                                break  # good enough for this stock; keep the scan fast
                    if not best:
                        return None
                    out = {
                        "symbol": symbol,
                        "price": best.get("price"),
                        "change_pct": best.get("change_pct"),
                        "move_date": best.get("date"),
                        "name": symbol,
                    }
                    if mode == "gappers":
                        out["gap_pct"] = best.get("change_pct")
                    return out
                # Single historical date: dedicated per-date helpers.
                from corporate_actions.sources import (
                    get_daily_change_on_date as _day_move,
                    get_gap_change_on_date as _gap_move,
                )

                move = (_gap_move if mode == "gappers" else _day_move)(exchange, symbol, target_date)
                if not move:
                    return None
                out = {
                    "symbol": symbol,
                    "price": move.get("price") or move.get("close"),
                    "change_pct": move.get("change_pct"),
                    "name": move.get("name") or symbol,
                }
                if move.get("date"):
                    out["move_date"] = str(move["date"])
                if move.get("gap_pct") is not None:
                    out["gap_pct"] = move.get("gap_pct")
                    out["change_pct"] = move.get("gap_pct")
                return out
            # Live screens: one retry pass on a None (transient Yahoo 429s
            # were silently blanking whole scans), with a tiny backoff.
            move = None
            for attempt in range(_retries + 1):
                move = fetch_period_change(symbol, period_tuple, exchange)
                if move:
                    break
                import time as _t
                _t.sleep(0.25 * (attempt + 1))
            if not move:
                return None
            return {
                "symbol": symbol,
                "price": move.get("price"),
                "change_pct": move.get("change_pct"),
                "change_pct_today": move.get("change_pct_today"),
                "name": move.get("name") or symbol,
            }
        except Exception as error:
            log.warning("_scan_one: %s", error)
            return None

    try:
        rows = await asyncio.to_thread(
            lambda: _scan_symbols(symbols, _scan_one, deadline, started)
        )
    except Exception as exc:
        log.exception("/api/movers scan failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    rows = [r for r in rows if r and r.get("change_pct") is not None]
    if mode == "gainers":
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
    elif mode == "losers":
        rows.sort(key=lambda r: r["change_pct"])
    else:
        rows.sort(key=lambda r: abs(r["change_pct"]), reverse=True)
    top = rows[:limit]

    # Enrich with the cached screener rows (valuation/momentum context).
    def _enrich(row: dict) -> dict:
        try:
            extra = screener_service._get_cached_row(row["symbol"])
        except Exception:
            extra = {}
        merged = dict(row)
        for field in ("company", "sector", "pe", "roe", "roce", "market_cap",
                      "div_yield", "rsi14", "macd_bull", "above_ema200"):
            if merged.get(field) is None and (extra or {}).get(field) is not None:
                merged[field] = extra[field]
        if not merged.get("company"):
            merged["company"] = merged.get("name") or merged["symbol"]
        return merged

    try:
        top = await asyncio.to_thread(lambda: list(map(_enrich, top)))
    except Exception as error:
        log.warning("_enrich: %s", error)
        pass
    # Preserve what this page view returned, timestamped (best-effort).
    storage.capture_command_output(
        "movers", "GET /api/movers", top, prefix="view_",
        mode=mode, universe=universe, period=period_key,
        date=target_date.isoformat() if target_date else None,
        date_from=range_from.isoformat() if range_from else None,
        date_to=range_to.isoformat() if range_to else None,
    )
    return JSONResponse({
        "mode": mode, "universe": universe, "period": period_key,
        "date": target_date.isoformat() if target_date else None,
        "date_from": range_from.isoformat() if range_from else None,
        "date_to": range_to.isoformat() if range_to else None,
        "count": len(top), "rows": top,
    })


def _scan_symbols(symbols: list[str], worker, deadline: float, started: float) -> list[dict]:
    """Bounded thread-pool fan-out shared by /api/movers (sync helper)."""
    import time as _time

    from concurrent.futures import ThreadPoolExecutor, as_completed

    from corporate_actions import config as _config

    # `deadline` is a duration; anchor it to THIS clock. The caller's
    # `started` uses the event-loop's monotonic clock while the old check
    # compared it against wall time - that always overflowed the deadline
    # and cut every movers scan to its first completed symbol (~0 rows).
    end_at = _time.time() + max(5.0, float(deadline))

    out: list[dict] = []
    max_workers = max(1, int(getattr(_config, "SCREENER_MAX_WORKERS", 12)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            if _time.time() > end_at:
                for pending in futures:
                    pending.cancel()
                break
            try:
                row = future.result()
            except Exception as error:
                log.warning("_scan_symbols: %s", error)
                continue
            if row:
                out.append(row)
    return out


@app.get("/api/openreport")
async def api_openreport(
    market: str = Query("all"), date: str = Query(""),
    refresh: bool = Query(False),
):
    """Opening/closing session screener (web twin of the bot's /openreport).

    market: all | in | us. date: optional YYYY-MM-DD for a historical session
    (that day's completed closes; a non-trading date is refused with a closed
    block - never substituted). Regular-session data only, official universes
    (Nifty 100 / Nifty 500 ex-100 / Nifty Microcap 250, US Mega/Large by
    market cap).

    REUSE-FIRST: when today's live report was already built once (Telegram,
    web or auto-refresh) the saved file is served instantly - the 60-90s
    scan runs only when nothing current exists, or refresh=1 is passed.
    Historical builds ARE saved under their date too, so a session built
    once is replayed from disk forever after (never re-fetched).
    """
    from corporate_actions.opening_report import report as openreport

    markets = {"all": ("in", "us"), "in": ("in",), "us": ("us",)}.get(market)
    if markets is None:
        raise HTTPException(status_code=400, detail="market must be all|in|us")
    target_date = None
    if (date or "").strip():
        import datetime as _dt

        try:
            target_date = _dt.date.fromisoformat(date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    # ---- reuse-first: serve the recorded file when it answers the ask ----
    if target_date is None and not refresh:
        try:
            existing = await asyncio.to_thread(storage.load_openclose)
            if openreport.recorded_covers(existing, markets):
                return JSONResponse(
                    {**(existing.get("report") or {}), "from_cache": True}
                )
        except Exception as exc:
            log.info("/api/openreport reuse check failed: %s", exc)
    if target_date is not None and not refresh:
        try:
            existing = await asyncio.to_thread(
                storage.load_openclose, target_date.isoformat())
            report = existing.get("report") if isinstance(existing, dict) else None
            if report:
                return JSONResponse({**report, "from_cache": True})
        except Exception as exc:
            log.info("/api/openreport historical reuse failed: %s", exc)

    report = await asyncio.to_thread(openreport.collect, markets, target_date)
    # Persist every build under its session date so the details are built
    # ONCE and reused afterwards (survives redeploys via the state push).
    try:
        from corporate_actions.opening_report.report import save_openclose_doc

        await asyncio.to_thread(save_openclose_doc, report, markets, target_date, "web")
    except Exception as exc:
        log.info("/api/openreport record skipped: %s", exc)
    return JSONResponse({**report, "from_cache": False})


@app.get("/api/openreport/recorded")
async def api_openreport_recorded(date: str = Query("")):
    """A recorded open+close session, date-wise, with no re-fetch.

    ?date=YYYY-MM-DD serves that session; omitted serves the latest.
    Response always carries `dates` (all recorded sessions, newest first)
    so the page can offer a history picker. {} (with dates possibly empty)
    when nothing was ever recorded.
    """
    try:
        day = (date or "").strip()
        if day:
            import datetime as _dt

            try:
                _dt.date.fromisoformat(day)
            except ValueError:
                raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
            doc = await asyncio.to_thread(storage.load_openclose, day)
            if not doc:
                raise HTTPException(status_code=404, detail=f"no recorded session for {day}")
        else:
            doc = await asyncio.to_thread(storage.load_openclose)
        dates = await asyncio.to_thread(storage.list_openclose_dates)
        return JSONResponse({**(doc or {}), "dates": dates[::-1]})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/checklist")
async def api_checklist(symbol: str | None = Query(None), market: str = Query("in")):
    """32-point investment scorecard (bot /checklist parity).

    Reuses the bot's own pure formatter; lines are Telegram-HTML (<b>,
    <code>) which browsers render as-is. Returns {"lines": [...]}.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    force_us = (market or "in").strip().lower() in ("us", "usa", "nasdaq", "nyse")
    try:
        from corporate_actions.formatting.checklist import format_checklist

        if force_us:
            quote = await asyncio.to_thread(lambda: sources.get_quote("US", key) or {}) or {}
            fund = await asyncio.to_thread(sources.get_us_fundamentals, key) or {}
            currency = "USD"
        else:
            try:
                quote = await asyncio.to_thread(sources.get_best_quote, "NSE", key) or {}
            except Exception:
                quote = await asyncio.to_thread(
                    lambda: sources.get_quote("NSE", key) or sources.get_quote("BSE", key) or {}
                ) or {}
            currency = "INR"
            is_us = quote.get("price") is None
            if is_us:
                probe = await asyncio.to_thread(lambda: sources.get_quote("US", key) or {}) or {}
                if probe.get("price") is not None or probe.get("name"):
                    quote, fund, currency = probe, await asyncio.to_thread(
                        sources.get_us_fundamentals, key) or {}, "USD"
                else:
                    fund = await asyncio.to_thread(sources.get_fundamentals, key, True) or {}
            else:
                fund = await asyncio.to_thread(sources.get_fundamentals, key, True) or {}
                try:
                    fund = sources.normalise_fundamentals(key, dict(fund or {}), quote or {})
                except Exception as error:
                    log.warning("_scan_symbols: %s", error)
                    pass
        if (quote.get("price") is None) and not fund:
            raise HTTPException(status_code=404, detail=f"no data for {key}")
        lines = await asyncio.to_thread(format_checklist, key, quote, fund, currency)
        return JSONResponse({"symbol": key, "currency": currency, "lines": lines})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/indicator")
async def api_indicator(symbol: str | None = Query(None), name: str | None = Query(None)):
    """Technical indicator deep-dive or full card (bot /indicator parity).

    ?name=RSI → one-indicator report; omitted → full all-indicators card
    (needs ~220 daily candles, otherwise 400 with a hint).
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper()
    try:
        from corporate_actions.scanner.indicator_report import (
            available_indicator_names,
            build_indicator_report,
            match_indicator,
        )

        ohlc = None
        exchange = None
        for candidate in ("NSE", "BSE", "US"):
            try:
                data = await asyncio.to_thread(sources.get_ohlc, candidate, key, "1d")
            except Exception:
                data = None
            if data and data.get("close") and len(data["close"]) >= 30:
                ohlc, exchange = data, candidate
                break
        if not ohlc:
            raise HTTPException(status_code=404, detail=f"no price history for {key}")
        if name:
            matched = match_indicator(name)
            if matched is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown indicator {name!r} (try: {available_indicator_names()})",
                )
            price = ohlc["close"][-1]
            open_price = (ohlc.get("open") or [None])[-1]
            change_pct = ((price / open_price) - 1.0) * 100.0 if open_price else None
            company = ohlc.get("name") or key
            currency = "$" if exchange == "US" else "\u20b9"
            lines = await asyncio.to_thread(
                build_indicator_report, key, company, price, change_pct, ohlc, matched, currency
            )
            return JSONResponse({"symbol": key, "indicator": matched, "lines": lines})
        if len(ohlc["close"]) < 220:
            raise HTTPException(
                status_code=400,
                detail=f"{key} has only {len(ohlc['close'])} daily candles — "
                "the full card needs ~220; try ?name=RSI instead",
            )
        try:
            import corporate_actions.scanner as scanner
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"scanner unavailable: {exc}")
        finding = await asyncio.to_thread(scanner.scan_stock, ohlc, None)
        if finding is None:
            raise HTTPException(status_code=500, detail=f"could not compute indicators for {key}")
        finding = await asyncio.to_thread(scanner.build_plan, finding)
        score, breakdown = await asyncio.to_thread(scanner.score_stock, finding)
        from corporate_actions.scanner.report import _detail_card_lines

        lines = await asyncio.to_thread(_detail_card_lines, finding, score, breakdown)
        return JSONResponse({"symbol": key, "score": score, "lines": lines})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
@app.get("/api/harmonic")
async def api_harmonic(symbol: str | None = Query(None), timeframe: str = Query("1d")):
    """Harmonic-pattern report for one symbol (bot /harmonicpatterns parity)."""
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    tf = (timeframe or "1d").strip().lower()
    try:
        from corporate_actions import harmonic as harmonic_mod

        result = None
        for candidate in ("NSE", "BSE", "US"):
            try:
                result = await asyncio.to_thread(
                    harmonic_mod.analyze, candidate, key, tf, None, False
                )
            except Exception:
                result = None
            if result:
                break
        if not result:
            raise HTTPException(status_code=404, detail=f"no harmonic data for {key}")
        try:
            lines = harmonic_mod.format_report(result)
        except Exception:
            lines = [str(result)]
        return JSONResponse({"symbol": key, "timeframe": tf, "lines": lines})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/metals")
async def api_metals():
    """Consensus gold/silver USD prices + per-source table (Commodities parity).

    Three free sources (CoinPaprika, currency-api, CoinGecko), closest-pair
    consensus, 5-minute server cache. Nulls when all are down — the UI then
    falls back to manual entry instead of blanking.
    """
    try:
        data = await asyncio.to_thread(sources.get_metals)
        return JSONResponse(data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/dividends")
async def api_dividends(symbol: str | None = Query(None), years: int = Query(5, ge=1, le=10)):
    """Cash-dividend history from Yahoo chart events (Dividend History parity).

    Returns per-payout events plus annual totals — the same source the
    reference app uses. Empty list (never an error) when Yahoo has nothing.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    try:
        data = await asyncio.to_thread(sources.get_dividends, "NSE", key, years)
        return JSONResponse({"symbol": key, **data})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/fundamentals")
async def api_fundamentals(symbol: str | None = Query(None), refresh: bool = Query(False), market: str = Query("in")):
    """Deep fundamentals for one symbol — the same dataset the Telegram bot's
    /fundamentalreport shows. Runs the bot's own sync fetch in a worker thread
    WITHOUT a timeout: a partial report helps nobody, and results are cached
    by the source layer anyway (first cold fetch can take ~20-30s while the
    screener.in tables are scraped; ?refresh=1 bypasses the cache).
    ?market=us serves a US ticker (/usstock parity): USD units, no screener.in.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    symbol = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO").removesuffix(".US")
    key = symbol
    log = logging.getLogger(__name__)
    want_us = (market or "in").strip().lower() in ("us", "usa", "nasdaq", "nyse")
    try:
        if refresh and not want_us:
            try:
                from corporate_actions.sources import fundamentals as fund_source
                fund_source._fund_cache.pop((key, True), None)
                fund_source._fund_cache.pop((key, False), None)
                log.info("/api/fundamentals: cache cleared for %s (refresh=1)", key)
            except Exception as error:
                log.warning("_scan_symbols: %s", error)
                pass
        if want_us:
            fund = await asyncio.to_thread(sources.get_us_fundamentals, key) or {}
            quote = await asyncio.to_thread(
                lambda: sources.get_quote("US", key) or {}
            )
            if _is_stub_fund(fund) and not (quote or {}).get("price"):
                raise HTTPException(status_code=404, detail=f"no data for {key} - symbol looks invalid")
            return JSONResponse({"symbol": key, "market": "us", "fund": fund, "quote": quote or {}})
        fund = await asyncio.to_thread(sources.get_fundamentals, key, True) or {}
        try:
            quote = await asyncio.to_thread(sources.get_best_quote, "NSE", key)
        except Exception:
            quote = None
        if not quote or quote.get("price") is None:
            try:
                quote = await asyncio.to_thread(
                    lambda: sources.get_quote("NSE", key) or sources.get_quote("BSE", key) or {}
                )
            except Exception:
                quote = quote or {}
        try:
            fund = sources.normalise_fundamentals(key, dict(fund or {}), quote or {})
        except Exception as error:
            log.warning("_scan_symbols: %s", error)
            pass
        # A stub-only merge (no quote, no fundamentals) means the symbol does
        # not resolve anywhere — tell the client plainly so it can gate the
        # watchlist/export actions instead of rendering a hollow report.
        if _is_stub_fund(fund) and not (quote or {}).get("price"):
            raise HTTPException(status_code=404, detail=f"no data for {key} - symbol looks invalid")
        return JSONResponse({"symbol": key, "market": "in", "fund": fund, "quote": quote or {}})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("/api/fundamentals failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fundamentals.csv")
async def api_fundamentals_csv(symbol: str | None = Query(None)):
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    try:
        key = (symbol or "").strip().upper().removesuffix(".NS").removesuffix(".BO")
        fund = sources.get_fundamentals(key, with_screener=True) or {}
        try:
            quote = sources.get_best_quote("NSE", key) or {}
        except Exception:
            quote = sources.get_quote("NSE", key) or sources.get_quote("BSE", key) or {}
        try:
            fund = sources.normalise_fundamentals(key, dict(fund or {}), quote or {})
        except Exception as error:
            log.warning("_scan_symbols: %s", error)
            pass
        import csv, io
        si = io.StringIO()
        writer = csv.writer(si)
        flat = {
            k: v for k, v in fund.items() if not isinstance(v, (dict, list))
        }
        writer.writerow(["field", "value"])
        writer.writerow(["symbol", key])
        writer.writerow(["company", quote.get("name") or fund.get("company") or fund.get("name") or ""])
        writer.writerow(["price", quote.get("price", "")])
        for k in sorted(flat):
            writer.writerow([k, flat[k]])
        return HTMLResponse(content=si.getvalue(), media_type="text/csv")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/corporate_actions")
async def api_corporate_actions(symbol: str | None = Query(None)):
    # aggregate NSE + BSE corporate actions for a symbol or recent window
    log = logging.getLogger(__name__)
    try:
        ca_timeout = float(os.getenv("CORP_ACTIONS_TIMEOUT", "8"))
        try:
            nse = await asyncio.wait_for(sources.get_nse_corporate_actions_async(symbol), timeout=ca_timeout)
        except asyncio.TimeoutError:
            log.warning("/api/corporate_actions: NSE lookup timed out for %s", symbol)
            nse = []
    except Exception:
        nse = []
    try:
        try:
            bse = await asyncio.wait_for(sources.get_bse_corporate_actions_async(), timeout=ca_timeout)
        except asyncio.TimeoutError:
            log.warning("/api/corporate_actions: BSE lookup timed out")
            bse = []
    except Exception:
        bse = []
    combined = [*nse, *bse]
    return JSONResponse(combined)


@app.get("/api/corporate_actions.csv")
async def api_corporate_actions_csv(symbol: str | None = Query(None)):
    try:
        nse = sources.get_nse_corporate_actions(symbol)
    except Exception:
        nse = []
    try:
        bse = sources.get_bse_corporate_actions()
    except Exception:
        bse = []
    combined = [*nse, *bse]
    import csv, io
    out = io.StringIO()

    def gen():
        w = csv.writer(out)
        if combined:
            keys = list(dict(combined[0]).keys())
            w.writerow(keys)
            yield out.getvalue()
            out.seek(0)
            out.truncate(0)
            for row in combined:
                w.writerow([row.get(k) for k in keys])
                yield out.getvalue()
                out.seek(0)
                out.truncate(0)
        else:
            yield ""

    return StreamingResponse(gen(), media_type="text/csv")


@app.get("/openreport", response_class=HTMLResponse)
async def openreport_page(request: Request):
    return templates.TemplateResponse(request, "openreport.html")


@app.get("/market", response_class=HTMLResponse)
async def market_page(request: Request):
    return templates.TemplateResponse(request, "market.html")


@app.get("/movers", response_class=HTMLResponse)
async def movers_page(request: Request):
    return templates.TemplateResponse(request, "movers.html")


@app.get("/forecast", response_class=HTMLResponse)
async def forecast_page(request: Request):
    return templates.TemplateResponse(request, "forecast.html")


@app.get("/checklist", response_class=HTMLResponse)
async def checklist_page(request: Request):
    return templates.TemplateResponse(request, "checklist.html")


@app.get("/indicator", response_class=HTMLResponse)
async def indicator_page(request: Request):
    return templates.TemplateResponse(request, "indicator.html")


@app.get("/news", response_class=HTMLResponse)
async def news_page(request: Request):
    return templates.TemplateResponse(request, "news.html")


@app.get("/invest", response_class=HTMLResponse)
async def invest_page(request: Request):
    return templates.TemplateResponse(request, "invest.html")


@app.get("/invest/stocks", response_class=HTMLResponse)
async def invest_stocks_page(request: Request):
    return templates.TemplateResponse(request, "invest_stocks.html")


@app.get("/invest/stocks/average", response_class=HTMLResponse)
async def invest_stock_average_page(request: Request):
    return templates.TemplateResponse(
        request, "invest_stock_average.html", context={"active": "average"}
    )


@app.get("/invest/stocks/profit", response_class=HTMLResponse)
async def invest_stock_profit_page(request: Request):
    return templates.TemplateResponse(
        request, "invest_stock_profit.html", context={"active": "profit"}
    )


@app.get("/invest/stocks/recovery", response_class=HTMLResponse)
async def invest_stock_recovery_page(request: Request):
    return templates.TemplateResponse(
        request, "invest_stock_recovery.html", context={"active": "recovery"}
    )


@app.get("/invest/stocks/pnl", response_class=HTMLResponse)
async def invest_stock_pnl_page(request: Request):
    return templates.TemplateResponse(
        request, "invest_stock_pnl.html", context={"active": "pnl"}
    )


@app.get("/invest/stocks/checklist", response_class=HTMLResponse)
async def invest_stock_checklist_page(request: Request):
    return templates.TemplateResponse(
        request, "invest_stock_checklist.html", context={"active": "checklist"}
    )


@app.get("/invest/mutual-funds", response_class=HTMLResponse)
async def invest_mutual_page(request: Request):
    return templates.TemplateResponse(request, "invest_mutual.html")


@app.get("/invest/bonds", response_class=HTMLResponse)
async def invest_bonds_page(request: Request):
    return templates.TemplateResponse(request, "invest_bonds.html")


@app.get("/invest/commodities", response_class=HTMLResponse)
async def invest_commodities_page(request: Request):
    return templates.TemplateResponse(request, "invest_commodities.html")


@app.get("/invest/ipo", response_class=HTMLResponse)
async def invest_ipo_page(request: Request):
    return templates.TemplateResponse(request, "invest_ipo.html")


@app.get("/fundamentals", response_class=HTMLResponse)
async def fundamentals_page(request: Request):
    return templates.TemplateResponse(request, "fundamentals.html")


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    return templates.TemplateResponse(request, "watchlist.html")


@app.get("/exdates", response_class=HTMLResponse)
async def exdates_page(request: Request):
    return templates.TemplateResponse(request, "exdates.html")


@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    return templates.TemplateResponse(request, "system.html")


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request):
    return templates.TemplateResponse(request, "sessions.html")


# Background snapshot recording state (one at a time; the web UI polls).
_snap_state: dict = {"recording": False, "error": None}


def _record_snapshot_thread(market: str, force: bool) -> None:
    try:
        snapshots_service.record_session_snapshot(
            market=market, force=force, recorded_by="web")
    except Exception as exc:
        log.warning("/api/snapshots/record failed: %s", exc)
        _snap_state["error"] = str(exc)
    finally:
        _snap_state["recording"] = False


@app.get("/api/snapshots")
async def api_snapshots(date: str = Query(""), market: str = Query("")):
    """The recorded session file(s) - reuse-first, never blocks the view.

    No date: the latest record (legacy single file). ?date=YYYY-MM-DD serves
    that session's ARCHIVED record ({} -> 404 when that day was never
    recorded) - recorded history is reusable at zero fetch cost. The
    response always carries `dates` (all archived sessions, newest first)
    so the page can offer a history picker. When the stored session is
    older than the market's latest session, a background auto-refresh is
    kicked once (reuse-first: current files are never re-fetched).
    """
    try:
        day = (date or "").strip()
        if day:
            import datetime as _dt

            try:
                _dt.date.fromisoformat(day)
            except ValueError:
                raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
            doc = await asyncio.to_thread(storage.load_snapshot_archive, day)
            if not doc:
                raise HTTPException(status_code=404, detail=f"no recorded session for {day}")
        else:
            doc = await asyncio.to_thread(storage.load_snapshots)
        dates = await asyncio.to_thread(storage.list_snapshot_dates)
        # Fire-and-forget auto-refresh: only fires when no explicit date was
        # requested (history views must not trigger fresh fetches) and the
        # file is stale (maybe_record_session_snapshot re-checks everything).
        if not day and not snapshots_service.is_recording():
            threading.Thread(
                target=snapshots_service.maybe_record_session_snapshot,
                kwargs={"market": str(market or (doc or {}).get("market") or "in"),
                        "recorded_by": "auto-page"},
                daemon=True, name="snapshot-auto-page",
            ).start()
        recording = bool(_snap_state["recording"]) or snapshots_service.is_recording()
        return JSONResponse({**(doc or {}), "dates": dates[::-1], "recording": recording})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/snapshots/record")
async def api_snapshots_record(
    market: str = Query("in"),
    universe: str | None = Query(None),
    force: bool = Query(False),
):
    """Start recording the last session in the background (202 at once).

    market in = India blocks (NIFTY 100 + 500 ex-100 + Microcap, like
    /openreport); us = Mega + Large-cap. Skips when a recording is already
    running or the file already covers the latest session (unless force=1)
    - the UI polls GET /api/snapshots.
    """
    text = f"{market or ''} {universe or ''}".lower()
    market = "us" if any(token in text for token in ("us", "nasdaq", "sp500", "s&p")) else "in"
    if _snap_state["recording"]:
        return JSONResponse({"started": False, "reason": "already recording"}, status_code=202)
    _snap_state["recording"] = True
    _snap_state["error"] = None
    thread = threading.Thread(
        target=_record_snapshot_thread, args=(market, force),
        daemon=True, name="snapshot-record",
    )
    thread.start()
    return JSONResponse({"started": True, "market": market}, status_code=202)


@app.get("/api/status")
async def api_status():
    """One call the UI uses to fill status cards on dashboard + system pages."""
    items = storage.load_watchlist()
    open_now = market_hours.is_market_open("in")
    info = market_hours.MARKETS.get("in", {})
    # Lightweight free-API health: probe the real fallback chain (Yahoo ->
    # NSE -> Stooq) once so the System/dashboard card reflects what users
    # actually get, instead of "Down" when only Stooq is unreachable.
    free_api_ok: bool | None = None
    free_api_source: str | None = None
    try:
        probe = await asyncio.to_thread(sources.get_best_quote, "NSE", "RELIANCE")
        free_api_ok = bool(probe and probe.get("price"))
        free_api_source = (probe or {}).get("source")
    except Exception:
        free_api_ok = False
    return JSONResponse({
        "version": app.version,
        "watchlist_count": len(items),
        "watchlist_preview": [str(it.get("symbol") or "") for it in items[:10]],
        "market_open": open_now,
        "market_text": f"{info.get('label', 'India')} \u00b7 {info.get('open', '09:15')}\u2013{info.get('close', '15:30')} IST",
        "telegram_configured": telegram_client.is_configured(),
        "sources": "Yahoo · NSE · Stooq · screener.in",
        "free_api_ok": free_api_ok,
        "free_api_source": free_api_source,
        "universe": "nifty500",
    })


@app.post("/api/telegram/test")
async def api_telegram_test():
    """Fire a real Telegram message so the user can verify the bot config."""
    log = logging.getLogger(__name__)
    if not telegram_client.is_configured():
        raise HTTPException(status_code=503, detail="Telegram is not configured")
    try:
        telegram_client.send_message("✅ Royal Stock dashboard test message — Telegram is working.")
        return JSONResponse({"ok": True})
    except Exception as exc:
        log.warning("/api/telegram/test failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------- admin -----
# Credential-gated management of user subscriptions, schedules and alert
# toggles. The gate (ADMIN_KEY env) lives in corporate_actions.admin; every
# endpoint below rejects non-admin requests BEFORE doing any work.


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin console page (client asks /api/admin/state for the gate state)."""
    return templates.TemplateResponse(request, "admin.html")


@app.get("/api/admin/state")
async def api_admin_state(token: str | None = Query(None)):
    """Login gate status + (when authenticated) the full admin payload."""
    if not admin_service.is_enabled():
        return JSONResponse({"enabled": False, "authenticated": False})
    if not admin_service.verify_session(token or ""):
        return JSONResponse({"enabled": True, "authenticated": False})
    users = await asyncio.to_thread(admin_service.list_users)
    schedules = await asyncio.to_thread(storage.load_schedule)
    return JSONResponse({
        "enabled": True,
        "authenticated": True,
        "users": list(users.values()),
        "schedules": schedules,
    })


@app.post("/api/admin/login")
async def api_admin_login(payload: dict):
    """Exchange the admin key for a session token (stored client-side)."""
    password = str((payload or {}).get("password") or "")
    token = await asyncio.to_thread(admin_service.create_session, password)
    if not token:
        await asyncio.sleep(0.3)  # blunt the brute-force rate a little
        raise HTTPException(status_code=401, detail="wrong admin key")
    return JSONResponse({"ok": True, "token": token})


@app.post("/api/admin/logout")
async def api_admin_logout(token: str | None = Query(None)):
    await asyncio.to_thread(admin_service.end_session, token or "")
    return JSONResponse({"ok": True})


def _admin_guard(token: str | None) -> None:
    """Raise 403 unless the request carries a live admin session."""
    reason = admin_service.check_token(token or "")
    if reason:
        raise HTTPException(status_code=403, detail=reason)


@app.get("/api/admin/user")
async def api_admin_user(token: str | None = Query(None), chat: str = Query(...)):
    """One chat's subscription list + schedule rows (admin only)."""
    _admin_guard(token)
    try:
        items = await asyncio.to_thread(admin_service.user_list, chat)
        schedule = await asyncio.to_thread(admin_service.user_schedule, chat)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse({"chat": chat, "items": items, "schedule": schedule})


@app.post("/api/admin/user/symbols")
async def api_admin_add_symbols(payload: dict):
    """Add symbols to a user's list (same validation as the watchlist tab)."""
    _admin_guard(payload.get("token"))
    chat = str(payload.get("chat") or "").strip()
    raw = payload.get("symbols")
    if not chat:
        raise HTTPException(status_code=400, detail="chat is required")
    if isinstance(raw, str):
        symbols = [part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()]
    elif isinstance(raw, list):
        symbols = [str(part).strip() for part in raw if str(part).strip()]
    else:
        symbols = []
    if not symbols:
        raise HTTPException(status_code=400, detail="provide symbols (list or comma/space separated string)")
    items = [{"symbol": s} for s in symbols]
    validated, rejected = await _resolve_watchlist_items(items)
    if rejected:
        raise HTTPException(status_code=422, detail="Unknown symbol(s): "
                            + ", ".join(r["symbol"] or "(empty)" for r in rejected))
    try:
        result = await asyncio.to_thread(admin_service.add_user_symbols, chat, validated)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse({
        "ok": True,
        "added": result.get("added"),
        "skipped_duplicates": result.get("skipped_duplicates"),
        "items": result.get("list", []),
    })


@app.delete("/api/admin/user/symbol")
async def api_admin_remove_symbol(
    token: str | None = Query(None),
    chat: str = Query(...),
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
):
    """Remove one symbol from a user's list."""
    _admin_guard(token)
    try:
        remaining = await asyncio.to_thread(
            admin_service.remove_user_symbol, chat, symbol, exchange)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse({"ok": True, "items": remaining, "count": len(remaining)})


@app.post("/api/admin/schedule")
async def api_admin_add_schedule(payload: dict):
    """Add a scheduled report for a user.

    Body: {token, chat, commands, interval_min, run_at?, market?} - the same
    fields /schedule add accepts, from the web instead of Telegram.
    """
    _admin_guard(payload.get("token"))
    chat = str(payload.get("chat") or "").strip()
    if not chat:
        raise HTTPException(status_code=400, detail="chat is required")
    raw = payload.get("commands")
    if isinstance(raw, str):
        commands = [part.strip() for part in re.split(r"[,\n]+", raw) if part.strip()]
    elif isinstance(raw, list):
        commands = [str(part).strip() for part in raw if str(part).strip()]
    else:
        commands = []
    try:
        interval = int(payload.get("interval_min") or 1440)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="interval_min must be a whole number of minutes")
    schedule = await asyncio.to_thread(
        admin_service.add_schedule, chat, interval, commands,
        payload.get("run_at"), payload.get("market"),
    )
    return JSONResponse({"ok": True, "schedule": schedule})


@app.delete("/api/admin/schedule")
async def api_admin_remove_schedule(
    token: str | None = Query(None),
    chat: str = Query(...),
    index: int = Query(...),
):
    """Remove the index-th (0-based) schedule row of a user."""
    _admin_guard(token)
    try:
        schedule = await asyncio.to_thread(
            admin_service.remove_schedule, chat, index)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse({"ok": True, "schedule": schedule})


@app.post("/api/admin/toggle")
async def api_admin_toggle(payload: dict):
    """Toggle a user's alerts from the admin console.

    Body: {token, chat, key, enabled} where key is one of:
      alerts    - master switch for ALL automatic pushes (/quiet equivalent)
      ca_alerts - corporate-action + ex-date reminder pushes only
    """
    _admin_guard(payload.get("token"))
    chat = str(payload.get("chat") or "").strip()
    key = str(payload.get("key") or "").strip()
    enabled = bool(payload.get("enabled"))
    if not chat:
        raise HTTPException(status_code=400, detail="chat is required")
    if key not in ("alerts", "ca_alerts"):
        raise HTTPException(status_code=400, detail="key must be 'alerts' or 'ca_alerts'")
    try:
        if key == "alerts":
            state = await asyncio.to_thread(
                admin_service.set_alerts_enabled, chat, enabled)
        else:
            state = await asyncio.to_thread(
                admin_service.set_ca_alerts, chat, enabled)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse({"ok": True, "user": state})


@app.post("/api/admin/user/email")
async def api_admin_user_email(payload: dict):
    """Save (or clear) a chat's mail id from the admin console.

    Body: {token, chat, email}. An empty email clears it. Same settings key
    the bot's /setemail uses, so the change is live in Telegram immediately.
    """
    _admin_guard(payload.get("token"))
    chat = str(payload.get("chat") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not chat:
        raise HTTPException(status_code=400, detail="chat is required")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="that does not look like an email address")

    def _save():
        settings = dict(storage.get_user_settings(chat) or {})
        if email:
            settings["email"] = email
        else:
            settings.pop("email", None)
            settings.pop("daily_email", None)  # a digest with no address is dead weight
        storage.save_user_settings(chat, settings)
        return admin_service._user_state(str(chat))

    try:
        state = await asyncio.to_thread(_save)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse({"ok": True, "user": state})


@app.get("/email", response_class=HTMLResponse)
async def page_email(request: Request):
    """Custom mail composer page (web twin of /email + /emailreport)."""
    try:
        return templates.TemplateResponse(request, "email.html")
    except Exception as exc:
        log.error("Template render failed: %s", exc)
        return HTMLResponse(_fallback_index_html())


@app.get("/api/email/status")
async def api_email_status():
    """Which sender is active (resend/smtp/none) - no secrets are exposed."""
    try:
        from corporate_actions.email import client as email_client

        return JSONResponse(await asyncio.to_thread(email_client.status))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/email/test")
async def api_email_test(payload: dict):
    """Send the styled test mail to one address (customizable destination)."""
    from corporate_actions.email import client as email_client

    to = str(payload.get("to") or payload.get("email") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="to is required")
    try:
        ok, info = await asyncio.to_thread(
            email_client.send_email,
            to,
            "Royal Stock: test mail ✅",
            ["✅ <b>Test mail OK</b> - reports will arrive here."],
            kind="web-test", chat_id="web",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail=info)
    return JSONResponse({"ok": True, "info": info})


@app.post("/api/email/send")
async def api_email_send(payload: dict):
    """Send any custom mail from the web composer.

    Body: {to, subject?, message} - `to` accepts comma separated mail ids
    (max 5), subject is optional, message is plain text (max 20000 chars).
    """
    from corporate_actions.email import client as email_client

    to = str(payload.get("to") or "").strip()
    subject = str(payload.get("subject") or "Royal Stock note").strip()
    message = str(payload.get("message") or payload.get("body") or "")
    if not to:
        raise HTTPException(status_code=400, detail="to is required")
    if not message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    try:
        ok, info = await asyncio.to_thread(
            email_client.send_custom, to, subject, message, kind="web-custom", chat_id="web")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail=info)
    return JSONResponse({"ok": True, "info": info})


@app.post("/api/email/report")
async def api_email_report(payload: dict):
    """Mail the deep fundamental report for one symbol (web /emailreport).

    Body: {to?, symbol, subject?} - `to` defaults to the owner's saved
    /setemail address; subject defaults to "Royal Stock report: SYMBOL".
    """
    from corporate_actions.email import client as email_client

    symbol = str(payload.get("symbol") or "").strip().upper().removesuffix(".NS").removesuffix(".BO")
    to = str(payload.get("to") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if not to:
        try:
            saved = await asyncio.to_thread(storage.get_user_settings, config_owner_chat())
            to = str((saved or {}).get("email") or "").strip()
        except Exception:
            to = ""
    if not to:
        raise HTTPException(status_code=400, detail="no destination: pass to or save /setemail first")
    try:
        from corporate_actions.analysis_service import build_analysis  # noqa: F401 (keeps parity import cheap)

        fund = await asyncio.to_thread(sources.get_fundamentals, symbol, True) or {}
        quote = await asyncio.to_thread(sources.get_best_quote, "NSE", symbol) or {}
        if not quote:
            quote = await asyncio.to_thread(
                lambda: sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}
            ) or {}
        if quote.get("price") is None and not fund:
            raise HTTPException(status_code=404, detail=f'"{symbol}" is not a valid stock symbol')
        from corporate_actions.formatting.stock_india import _fund_report_lines

        lines = _fund_report_lines(symbol, quote, fund, include_tip=False)
        if subject:
            import html as _html

            lines = [f"<i>{_html.escape(subject)}</i>", ""] + lines
        ok, info = await asyncio.to_thread(
            email_client.send_email, to, subject or f"Royal Stock report: {symbol}", lines,
            kind="web-report", chat_id="web",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail=info)
    return JSONResponse({"ok": True, "info": info, "to": to})


def config_owner_chat():
    """Owner chat id for the default mail destination (never raises)."""
    try:
        from corporate_actions import config as _config

        return str(_config.TELEGRAM_CHAT_ID or "local")
    except Exception:
        return "local"


@app.post("/api/email/open")
async def api_email_open(payload: dict):
    """Mail the opening-session screener tables (recorded file, colorful).

    Body: {to?, date?} - `to` defaults to the owner's saved /setemail
    address; `date` (YYYY-MM-DD) mails that recorded session, otherwise the
    latest recorded session.
    """
    from corporate_actions.email import client as email_client
    from corporate_actions.email.daily import _as_report, build_open_lines

    to = str(payload.get("to") or "").strip()
    date = str(payload.get("date") or "").strip()
    if not to:
        try:
            saved = await asyncio.to_thread(storage.get_user_settings, config_owner_chat())
            to = str((saved or {}).get("email") or "").strip()
        except Exception:
            to = ""
    if not to:
        raise HTTPException(status_code=400, detail="no destination: pass to or save /setemail first")
    try:
        doc = await asyncio.to_thread(storage.load_openclose, date or None)
        report = _as_report(doc)
        if not report.get("sections"):
            raise HTTPException(status_code=404, detail="no recorded session yet - run /openreport first")
        label = str(report.get("target_date") or (doc.get("recorded_at") or "")[:10] or "session")
        ok, info = await asyncio.to_thread(
            email_client.send_email, to,
            f"Royal Stock opening: {label}", build_open_lines(report, label),
            kind="web-open", chat_id="web",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail=info)
    return JSONResponse({"ok": True, "info": info, "to": to})


@app.post("/api/email/eod")
async def api_email_eod(payload: dict):
    """Mail the closing screener + end-of-day stored-details tables.

    Body: {to?, chat?, date?} - `to` defaults to the owner's /setemail
    address; `chat` picks whose watchlist/schedule/settings are tabulated
    (defaults to the owner); `date` picks a recorded session.
    """
    from corporate_actions.email import client as email_client
    from corporate_actions.email.daily import _as_report, build_close_lines, build_eod_store_lines
    from corporate_actions.email.daily import fetch_watchlist_quotes

    to = str(payload.get("to") or "").strip()
    chat = str(payload.get("chat") or config_owner_chat()).strip()
    date = str(payload.get("date") or "").strip()
    if not to:
        try:
            saved = await asyncio.to_thread(storage.get_user_settings, chat)
            to = str((saved or {}).get("email") or "").strip()
        except Exception:
            to = ""
    if not to:
        raise HTTPException(status_code=400, detail="no destination: pass to or save /setemail first")
    try:
        doc = await asyncio.to_thread(storage.load_openclose, date or None)
        report = _as_report(doc)
        lines: list = []
        label = str(report.get("target_date") or (doc.get("recorded_at") or "")[:10] or "session")
        if report.get("sections"):
            lines.extend(build_close_lines(report, label))
        try:
            watchlist = await asyncio.to_thread(storage.load_watchlist)
            quotes = await asyncio.to_thread(fetch_watchlist_quotes, watchlist)
        except Exception as error:
            log.warning("api_email_eod quotes skipped: %s", error)
            quotes = []
        lines.extend(await asyncio.to_thread(build_eod_store_lines, chat, quotes))
        ok, info = await asyncio.to_thread(
            email_client.send_email, to, f"Royal Stock close + EOD: {label}", lines,
            kind="web-eod", chat_id="web",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail=info)
    return JSONResponse({"ok": True, "info": info, "to": to})


@app.post("/api/email/both")
async def api_email_both(payload: dict):
    """Mail open + close session + EOD stores in ONE mail (web /emailboth).

    Body: {to?, chat?, date?} - same defaults as /api/email/eod.
    """
    from corporate_actions.email import client as email_client
    from corporate_actions.email.daily import _as_report, build_combined_lines
    from corporate_actions.email.daily import fetch_watchlist_quotes

    to = str(payload.get("to") or "").strip()
    chat = str(payload.get("chat") or config_owner_chat()).strip()
    date = str(payload.get("date") or "").strip()
    if not to:
        try:
            saved = await asyncio.to_thread(storage.get_user_settings, chat)
            to = str((saved or {}).get("email") or "").strip()
        except Exception:
            to = ""
    if not to:
        raise HTTPException(status_code=400, detail="no destination: pass to or save /setemail first")
    try:
        doc = await asyncio.to_thread(storage.load_openclose, date or None)
        report = _as_report(doc)
        if not report.get("sections"):
            raise HTTPException(status_code=404, detail="no recorded session yet - run /openreport first")
        label = str(report.get("target_date") or (doc.get("recorded_at") or "")[:10] or "session")
        try:
            watchlist = await asyncio.to_thread(storage.load_watchlist)
            quotes = await asyncio.to_thread(fetch_watchlist_quotes, watchlist)
        except Exception as error:
            log.warning("api_email_both quotes skipped: %s", error)
            quotes = []
        lines = await asyncio.to_thread(build_combined_lines, chat, report, label, quotes)
        ok, info = await asyncio.to_thread(
            email_client.send_email, to, f"Royal Stock open + close + EOD: {label}", lines,
            kind="web-both", chat_id="web",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail=info)
    return JSONResponse({"ok": True, "info": info, "to": to})


@app.get("/api/email/log")
async def api_email_log(limit: int = Query(10, ge=1, le=50)):
    """Recent mail sends, newest first: ✅ success or ❌ failed + reason."""
    try:
        entries = await asyncio.to_thread(storage.load_mail_log, limit)
        return JSONResponse({"entries": entries, "count": len(entries)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
