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
    </style>
  </head>
  <body>
    <div class=\"wrap\">
    <h1>📈 Royal Stock</h1>
      <p class=\"muted\">Custom dashboard · no Streamlit shell</p>
      <p>The dashboard is running, but the template renderer could not load the HTML shell.</p>
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
        return templates.TemplateResponse("index.html", {"request": request})
    except Exception as exc:
        # log the template rendering error for debugging
        log = logging.getLogger(__name__)
        log.error("Template render failed: %s", exc)
        log.debug(traceback.format_exc())
        # attempt to serve the raw index.html file as a last resort so UI still loads
        try:
            with open(os.path.join(BASE_DIR, "templates", "index.html"), "r", encoding="utf-8") as f:
                return HTMLResponse(f.read())
        except Exception:
            return HTMLResponse(_fallback_index_html())


@app.get("/api/watchlist")
async def api_watchlist():
    return JSONResponse(storage.load_watchlist())


@app.post("/api/watchlist")
async def add_watchlist(payload: dict):
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    result = storage.add_to_watchlist(items)
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
    try:
        rows = await asyncio.wait_for(
            screen_universe_async(universe=universe, filters=filters, sort=sort, ascending=ascending, limit=limit, offset=offset),
            timeout=timeout,
        )
        return JSONResponse(rows)
    except asyncio.TimeoutError:
        log.warning("/api/screener timed out after %s seconds", timeout)
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
    try:
        rows = await asyncio.wait_for(
            screen_universe_async(universe=universe, filters=filters, sort=sort, ascending=ascending, limit=limit, offset=offset),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("/api/screener.csv timed out after %s seconds", timeout)
        raise HTTPException(status_code=504, detail="screener csv timeout")
    except Exception as e:
        log.exception("/api/screener.csv failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    import csv
    import io

    cols = ["symbol", "company", "price", "change_pct", "pe", "roe", "market_cap", "rsi14"]

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


@app.get("/api/fundamentals")
async def api_fundamentals(symbol: str | None = Query(None)):
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    log = logging.getLogger(__name__)
    try:
        fund_timeout = float(os.getenv("FUND_API_TIMEOUT", "10"))
        quote_timeout = float(os.getenv("QUOTE_API_TIMEOUT", "6"))
        try:
            fund = await asyncio.wait_for(sources.get_fundamentals_async(symbol, True), timeout=fund_timeout) or {}
        except asyncio.TimeoutError:
            log.warning("/api/fundamentals: get_fundamentals_async timed out for %s", symbol)
            fund = {}
        try:
            quote = await asyncio.wait_for(sources.get_quote_async("NSE", symbol), timeout=quote_timeout)
            if not quote:
                quote = await asyncio.wait_for(sources.get_quote_async("BSE", symbol), timeout=quote_timeout)
        except asyncio.TimeoutError:
            log.warning("/api/fundamentals: quote lookup timed out for %s", symbol)
            quote = {}
        return JSONResponse({"symbol": symbol, "fund": fund, "quote": quote})
    except Exception as e:
        log.exception("/api/fundamentals failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/fundamentals.csv")
async def api_fundamentals_csv(symbol: str | None = Query(None)):
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    try:
        fund = sources.get_fundamentals(symbol, with_screener=True) or {}
        quote = sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}
        import csv, io
        si = io.StringIO()
        writer = csv.writer(si)
        keys = [
            "symbol",
            "company",
            "price",
            "market_cap",
            "pe",
            "roe",
            "debt_to_equity",
        ]
        writer.writerow(keys)
        writer.writerow([
            symbol,
            fund.get("company") or fund.get("name") or "",
            quote.get("price"),
            fund.get("market_cap"),
            fund.get("pe"),
            fund.get("roe"),
            fund.get("debt_to_equity"),
        ])
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
    return templates.TemplateResponse("market.html", {"request": request})


@app.get("/fundamentals", response_class=HTMLResponse)
async def fundamentals_page(request: Request):
    return templates.TemplateResponse("fundamentals.html", {"request": request})


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    return templates.TemplateResponse("watchlist.html", {"request": request})


@app.get("/exdates", response_class=HTMLResponse)
async def exdates_page(request: Request):
    return templates.TemplateResponse("exdates.html", {"request": request})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
