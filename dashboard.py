"""Custom web dashboard for the Stock Alert Bot.

This is the modern, non-Streamlit app shell. It serves a real HTML dashboard
and exposes the core data endpoints used by the UI.
"""
from __future__ import annotations

import math
import os
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import logging
import traceback

from corporate_actions import sources, storage
from corporate_actions.screener_service import screen_universe, screen_universe_async
from corporate_actions.market import hours as market_hours
from corporate_actions.telegram import client as telegram_client
import asyncio
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Royal Stock", version="2.0.0")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# configure basic logging so exceptions are visible in the server logs
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)


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
    except Exception:
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
        except Exception:
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


@app.get("/", response_class=HTMLResponse)
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


@app.post("/api/watchlist")
async def add_watchlist(payload: dict):
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    result = storage.add_to_watchlist([{**item, "exchange": (item.get("exchange") or "NSE").upper()} for item in items])
    return JSONResponse({"items": result, "count": len(result)})


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


@app.get("/health")
async def health():
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
    timeframe: str = Query("1d"),
):
    """OHLC history for the fundamentals price chart (Yahoo -> Stooq).

    Returns {'symbol','bars': {timestamp,open,high,low,close,volume,...},
    'source'} with at most ~260 daily bars, downsampled server-side when the
    full 1y series would bloat the response.
    """
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    ex = (exchange or "NSE").strip().upper()
    tf = (timeframe or "1d").strip().lower()
    if tf not in ("1d", "1w", "1mo"):
        tf = "1d"
    try:
        bars = await asyncio.to_thread(sources.get_ohlc, ex, key, tf)
        source = "yahoo"
        if not bars or not bars.get("close"):
            try:
                bars = await asyncio.to_thread(sources.get_stooq_history, key, ex)
                source = "stooq"
            except Exception:
                bars = None
        if not bars:
            return JSONResponse({"symbol": key, "bars": None, "source": "none"})
        # Downsample to <= 160 points so the canvas chart stays fast.
        closes = bars.get("close") or []
        if len(closes) > 160:
            step = max(1, len(closes) // 160)
            for field in ("timestamp", "open", "high", "low", "close", "volume"):
                values = bars.get(field) or []
                bars[field] = values[::step]
        bars["source"] = bars.get("source") or source
        return JSONResponse({"symbol": key, "bars": bars, "source": bars.get("source")})
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
    """Index constituent symbols (NIFTY 500 / NIFTY 100 / NASDAQ 100 / S&P 500)."""
    try:
        symbols = await asyncio.to_thread(sources.get_index_universe, universe)
        return JSONResponse({"universe": universe, "count": len(symbols or []), "symbols": symbols or []})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/fundamentals")
async def api_fundamentals(symbol: str | None = Query(None), refresh: bool = Query(False)):
    """Deep fundamentals for one symbol — the same dataset the Telegram bot's
    /fundamentalreport shows. Runs the bot's own sync fetch in a worker thread
    WITHOUT a timeout: a partial report helps nobody, and results are cached
    by the source layer anyway (first cold fetch can take ~20-30s while the
    screener.in tables are scraped; ?refresh=1 bypasses the cache)."""
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    key = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
    log = logging.getLogger(__name__)
    try:
        if refresh:
            try:
                from corporate_actions.sources import fundamentals as fund_source
                fund_source._fund_cache.pop((key, True), None)
                fund_source._fund_cache.pop((key, False), None)
                log.info("/api/fundamentals: cache cleared for %s (refresh=1)", key)
            except Exception:
                pass
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
        except Exception:
            pass
        return JSONResponse({"symbol": key, "fund": fund, "quote": quote or {}})
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
        except Exception:
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


@app.get("/market", response_class=HTMLResponse)
async def market_page(request: Request):
    return templates.TemplateResponse(request, "market.html")


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


@app.get("/api/status")
async def api_status():
    """One call the UI uses to fill status cards on dashboard + system pages."""
    items = storage.load_watchlist()
    open_now = market_hours.is_market_open("in")
    info = market_hours.MARKETS.get("in", {})
    # Lightweight free-API health: one cheap Stooq fetch so System tab can
    # show whether live quotes are reachable without hammering Yahoo.
    free_api_ok: bool | None = None
    try:
        probe = await asyncio.to_thread(sources.get_stooq_quote, "RELIANCE", "NSE")
        free_api_ok = bool(probe and probe.get("price"))
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
