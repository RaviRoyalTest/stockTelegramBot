"""Tab verification: routes, templates, aliases, filters. Run with python."""
import pathlib

import dashboard
from corporate_actions.sources.free_api import normalise_fundamentals
from corporate_actions.sources.screener_parsing import parse_company_name
from corporate_actions import screener_service

print("== routes ==")
paths = sorted({r.path for r in dashboard.app.routes})
for must in ["/api/quote", "/api/history", "/api/news", "/api/search",
             "/api/universe", "/api/screener", "/api/fundamentals",
             "/api/watchlist", "/api/status", "/api/corporate_actions",
             "/", "/market", "/fundamentals", "/watchlist", "/exdates", "/system"]:
    print(("OK  " if must in paths else "MISS"), must)

print("== fundamentals template ids ==")
t = pathlib.Path("templates/fundamentals.html").read_text(encoding="utf-8")
for i in ["rTitle", "rPrice", "rHealth", "rNews", "rCA", "rChart",
          "rSources", "rCompleteness", "symbolList", "rCashYear"]:
    print(("OK  " if f'id="{i}"' in t else "MISS"), i)

print("== market template ids/filters ==")
m = pathlib.Path("templates/market.html").read_text(encoding="utf-8")
for i in ["peMin", "peMax", "roeMin", "roceMin", "divMin", "debtMax",
          "mcapMin", "priceMin", "rsiMin", "rsiMax", "chgMin", "sector",
          "symContains", "macdBull", "aboveEma", "presetRow",
          "saveFilters", "resCount", "trendBadges"]:
    print(("OK  " if i in m else "MISS"), i)

print("== base toast-stack ==")
b = pathlib.Path("templates/base.html").read_text(encoding="utf-8")
print("OK   toast-stack" if "toast-stack" in b else "MISS toast-stack")

print("== normalise aliases ==")
f = {"pe": 20, "rsi": 55.0, "macd_line": 1.0, "macd_signal": 0.5,
     "sma_200": 100.0, "mcap_cr": 5000.0, "promoter_pct": "50.2%"}
q = {"price": 110.0, "name": "Test Co", "source": "yahoo"}
out = normalise_fundamentals("TEST", dict(f), dict(q))
checks = [out.get("company") == "Test Co",
          out.get("market_cap") == 5000.0,
          out.get("rsi14") == 55.0,
          out.get("macd_bull") is True,
          out.get("above_ema200") is True,
          out.get("promoter_pct_num") == 50.2]
print("OK   normalise" if all(checks) else f"FAIL normalise {out}")

print("== company parse ==")
print("OK   parse_company_name"
      if parse_company_name("<h1>Reliance Industries Ltd</h1>") == "Reliance Industries Ltd"
      else "FAIL parse_company_name")

print("== screener filters/sort ==")
rows = [
    {"symbol": "A", "pe": 10, "roe": 20, "roce": 20, "div_yield": 2.0,
     "market_cap": 5000, "price": 100, "rsi14": 60,
     "macd_bull": True, "above_ema200": True},
    {"symbol": "B", "pe": 30, "roe": 5, "roce": 5, "div_yield": 0.2,
     "market_cap": 500, "price": 20, "rsi14": 40,
     "macd_bull": False, "above_ema200": False},
]
f1 = screener_service._apply_filters(rows, {"roce_min": 15, "div_yield_min": 1.0})
s1 = screener_service._sort_rows(rows, "div_yield", False)
print("OK   filters" if [r["symbol"] for r in f1] == ["A"] else f"FAIL filters {f1}")
print("OK   sort" if [r["symbol"] for r in s1] == ["A", "B"] else f"FAIL sort {s1}")

print("== screener row build (mocked sources) ==")
import corporate_actions.screener_service as svc
orig_fund = svc.sources.get_fundamentals
orig_best = getattr(svc.sources, "get_best_quote", None)
svc.sources.get_fundamentals = lambda s, with_screener=True: {"pe": 12, "rsi": 55,
    "macd_hist": 0.5, "sma_200": 90, "mcap_cr": 8000, "company": "Mock Co"}
svc.sources.get_best_quote = lambda e, s: {"price": 100, "change_pct": 1.5,
    "name": "Mock Co", "source": "yahoo"}
try:
    row = svc._build_row("MOCK")
    ok = (row["rsi14"] == 55 and row["macd_bull"] is True
          and row["above_ema200"] is True and row["market_cap"] == 8000
          and row["price"] == 100 and "div_yield" in row and "roce" in row)
    print("OK   build_row" if ok else f"FAIL build_row {row}")
finally:
    svc.sources.get_fundamentals = orig_fund
    if orig_best is not None:
        svc.sources.get_best_quote = orig_best

print("DONE")
