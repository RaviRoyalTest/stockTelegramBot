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

print("== new web routes ==")
for must in ["/api/movers", "/api/analysis", "/api/checklist", "/api/indicator",
             "/api/harmonic", "/api/metals", "/api/dividends", "/movers", "/forecast", "/checklist",
             "/indicator", "/news", "/invest", "/invest/stocks",
             "/invest/stocks/average", "/invest/stocks/profit", "/invest/stocks/recovery",
             "/invest/stocks/pnl", "/invest/stocks/checklist",
             "/invest/mutual-funds", "/invest/bonds", "/invest/commodities"]:
    print(("OK  " if must in paths else "MISS"), must)

print("== new web templates ==")
for name in ["movers.html", "forecast.html", "checklist.html",
             "indicator.html", "news.html", "invest.html",
             "invest_stocks.html", "invest_stock_average.html",
             "invest_stock_profit.html", "invest_stock_recovery.html",
             "invest_stock_pnl.html", "invest_stock_checklist.html",
             "invest_mutual.html", "invest_bonds.html",
             "invest_commodities.html"]:
    p = pathlib.Path("templates") / name
    print(("OK  " if p.exists() else "MISS"), name)

print("== invest content ==")
stocks = pathlib.Path("templates/invest_stocks.html").read_text(encoding="utf-8")
for needle in ["/invest/stocks/average", "/invest/stocks/profit", "/invest/stocks/recovery",
               "/invest/stocks/pnl", "/invest/stocks/checklist"]:
    print(("OK  " if needle in stocks else "MISS"), "stocks-hub:" + needle)
avg = pathlib.Path("templates/invest_stock_average.html").read_text(encoding="utf-8")
for needle in ["avgCalcMode", "avgCurMode", "avgBuyMode", "avgTargetPane", "_stock_tools_nav.html"]:
    print(("OK  " if needle in avg else "MISS"), "average:" + needle)
profit = pathlib.Path("templates/invest_stock_profit.html").read_text(encoding="utf-8")
for needle in ["ptBtn", "Overall XIRR", "simModal", "Annualized Return"]:
    print(("OK  " if needle in profit else "MISS"), "profit:" + needle)
rec = pathlib.Path("templates/invest_stock_recovery.html").read_text(encoding="utf-8")
for needle in ["Recovery % = [1", "recSev", "Catastrophic"]:
    print(("OK  " if needle in rec else "MISS"), "recovery:" + needle)
pnl = pathlib.Path("templates/invest_stock_pnl.html").read_text(encoding="utf-8")
for needle in ["Custom Realised Stock Value", "Equity", "/api/quote"]:
    print(("OK  " if needle in pnl else "MISS"), "pnl:" + needle)
check = pathlib.Path("templates/invest_stock_checklist.html").read_text(encoding="utf-8")
for needle in ["Copy All", "investStockChecklist", "Personal — First Principles"]:
    print(("OK  " if needle in check else "MISS"), "checklist:" + needle)
mf = pathlib.Path("templates/invest_mutual.html").read_text(encoding="utf-8")
for needle in ["itype", "swpYrs", "istart", "Jump to SWP Start", "Value at Term End",
               "viewBtn", "mfChecks", "Sharpe ratio"]:
    print(("OK  " if needle in mf else "MISS"), "mutual:" + needle)
bonds = pathlib.Path("templates/invest_bonds.html").read_text(encoding="utf-8")
for needle in ["beforeChecks", "bondChecks", "Investment grade credit rating"]:
    print(("OK  " if needle in bonds else "MISS"), "bonds:" + needle)
comm = pathlib.Path("templates/invest_commodities.html").read_text(encoding="utf-8")
for needle in ["rRatio", "/api/metals", "Historical Context", "Investment Guidelines"]:
    print(("OK  " if needle in comm else "MISS"), "commodities:" + needle)

print("== metals consensus ==")
from corporate_actions.sources.metals import _consensus

checks = [
    _consensus([]) is None,
    _consensus([2000.0]) == 2000.0,
    _consensus([2000.0, 2020.0]) == 2010.0,
    _consensus([2000.0, 2005.0, 2100.0]) == 2002.5,
]
print("OK   metals_consensus" if all(checks) else f"FAIL metals_consensus {checks}")

print("== fundamentals extras ==")
fx = pathlib.Path("templates/fundamentals.html").read_text(encoding="utf-8")
for needle in ["rAboutCard", "rDiv", "/api/dividends", "rScore", "How is this score calculated?",
               "business_summary", "healthScore"]:
    print(("OK  " if needle in fx else "MISS"), "fundamentals:" + needle)

print("== nav links ==")
nb = pathlib.Path("templates/base.html").read_text(encoding="utf-8")
for link in ['href="/movers"', 'href="/forecast"', 'href="/checklist"',
             'href="/indicator"', 'href="/news"', 'href="/invest"',
             'topSearchInput']:
    print(("OK  " if link in nb else "MISS"), link)
for fname, needle in [("fundamentals.html", "recentPills"),
                      ("index.html", "recentPills"),
                      ("fundamentals.html", "pushRecent")]:
    content = pathlib.Path("templates") / fname
    text = content.read_text(encoding="utf-8")
    print(("OK  " if needle in text else "MISS"), f"{fname}:{needle}")
    print(("OK  " if link in nb else "MISS"), link)

print("== bot report extras ==")
from corporate_actions.formatting.report_extras import (
    company_name,
    financial_health_lines,
    peers_lines,
    quote_source_tag,
    rsi_value,
    sources_footer_lines,
)
fund = {"company": "Acme Ltd", "rsi14": 55.0, "market_cap": 8000.0,
        "current_ratio": 1.8, "free_cashflow": 5e9,
        "competitors": [{"name": "Rival", "price": 100.0, "pe": 12.0}],
        "data_sources": ["screener.in"]}
quote = {"price": 110.0, "source": "nse"}
checks = [
    company_name("ACME", {}, fund) == "Acme Ltd",
    rsi_value(fund) == 55.0,
    any("Current Ratio" in line for line in financial_health_lines(fund)),
    any("Rival" in line for line in peers_lines(fund)),
    "nse" in quote_source_tag(quote),
    any("screener.in" in line for line in sources_footer_lines(quote, fund)),
    financial_health_lines({}) == [] and peers_lines({}) == [],
]
print("OK   report_extras" if all(checks) else f"FAIL report_extras {checks}")

print("== analysis service ==")
from corporate_actions.analysis_service import build_analysis

demo_fund = {"pe": 50.0, "roe": 5.0, "roce": 6.0, "debt_to_equity": 1.5,
             "macd_line": 1.0, "macd_signal": 2.0, "sma_50": 120.0,
             "sma_200": 130.0, "fii_pct": "10.0% (-0.50%)",
             "annuals": [{"year": "Mar 2024", "sales": 100.0, "sales_growth": 12.0,
                          "net_profit": 10.0, "profit_growth": 8.0}],
             "cash_flow": {"free_cash_flow": -5.0, "cfo": -2.0}}
analysis = build_analysis(demo_fund, 100.0)
checks = [
    isinstance(analysis.get("snapshot"), list) and analysis["snapshot"],
    isinstance(analysis.get("verdict"), str) and analysis["verdict"],
    any("P/E" in c for c in analysis["concerns"]),
    any("sales" in p for p in analysis["positives"]),
    isinstance(analysis.get("main_question"), list) and analysis["main_question"],
]
print("OK   analysis_service" if all(checks) else f"FAIL analysis_service {analysis}")

print("== /screen parser ==")
from corporate_actions.bot.screen_commands import parse_screen_args

f, s, asc, lim, uni = parse_screen_args(["pe<25", "roe>15"])
checks = [f.get("pe_max") == 25 and f.get("roe_min") == 15 and lim == 10]
f2, s2, asc2, lim2, uni2 = parse_screen_args(["rsi", "50-70", "macd", "n", "15", "sector", "bank", "sort", "pe"])
checks.append(f2.get("rsi_min") == 50 and f2.get("rsi_max") == 70
              and f2.get("require_macd_bull") is True and lim2 == 15
              and f2.get("sector") == "bank" and s2 == "pe" and asc2 is True)
f3, _, _, _, uni3 = parse_screen_args(["div>1.5", "nifty100"])
checks.append(f3.get("div_yield_min") == 1.5 and uni3 == "nifty100")
print("OK   screen_parser" if all(checks) else f"FAIL screen_parser {checks}")

print("== deep report keeps everything + adds sections ==")
from corporate_actions.formatting.stock_india_report import _fund_report_lines

rich_fund = dict(demo_fund)
rich_fund.update({"company": "Acme Ltd", "sector": "Tech", "industry": "Software",
                  "rsi": 55.0, "pe": 18.0, "forward_pe": 16.0, "sector_pe": 22.0,
                  "price_to_book": 3.0, "trailing_eps": 5.0, "book_value": 40.0,
                  "shares_outstanding": 1e9, "roe": 16.0, "roce": 18.0,
                  "profit_margin": 0.12, "revenue_growth": 0.10,
                  "current_ratio": 1.8, "operating_cashflow": 8e9,
                  "free_cashflow": 5e9, "interest_coverage_ratio": 4.0,
                  "debt_to_equity": 0.3, "target_mean": 130.0,
                  "num_analysts": 10, "rec_mean": 2.0, "rec_key": "buy",
                  "rec_trend": {"strong_buy": 2, "buy": 5, "hold": 3, "sell": 0, "strong_sell": 0},
                  "promoter_pct": "50.0%", "officers": [{"name": "Jane", "title": "CEO"}],
                  "competitors": [{"name": "Rival", "price": 100.0}],
                  "annuals": [{"year": "Mar 2024", "sales": 100.0, "sales_growth": 12.0,
                               "opm": 20.0, "net_profit": 10.0, "profit_growth": 8.0,
                               "eps": 5.0, "roce": 18.0}],
                  "quarters": [{"quarter": "Jun 2024", "sales": 30.0, "opm": 21.0,
                                "net_profit": 3.0, "eps": 1.5}],
                  "balance_sheet": {"net_worth": 500.0, "borrowings": 100.0, "total_assets": 900.0},
                  "cash_flow": {"year": "Mar 2024", "cfo": 8.0, "cfi": -2.0, "cff": -1.0,
                                "net_cash_flow": 5.0, "free_cash_flow": 4.0},
                  "data_sources": ["screener.in"]})
text = "\n".join(_fund_report_lines("ACME", {"price": 110.0, "change_pct": 1.5,
                                             "name": "Acme Ltd", "source": "nse"}, rich_fund))
must_keep = ["FUNDAMENTAL REPORT", "PRICE & MOVEMENT", "TECHNICAL INDICATORS",
             "VALUATION", "GROWTH & MARGINS", "PER-SHARE & SCALE",
             "BALANCE SHEET", "CASH FLOW", "RETURNS", "5-YEAR PERFORMANCE",
             "QUARTERLY RESULTS", "ANALYST VIEW", "SHAREHOLDING",
             "TOP MANAGEMENT", "FUNDAMENTAL SNAPSHOT", "KEY CONCERNS",
             "KEY POSITIVES", "OVERALL VIEW", "VERDICT", "Main Question",
             "END OF REPORT"]
must_add = ["FINANCIAL HEALTH", "TOP COMPETITORS", "Data:"]
missing = [m for m in must_keep + must_add if m not in text]
print("OK   deep_report" if not missing else f"FAIL deep_report missing {missing}")

print("== grouped nav ==")
nb2 = pathlib.Path("templates/base.html").read_text(encoding="utf-8")
for needle in ["nav-group", "nav-drop", "nav-menu", 'href="/invest/stocks',
               'href="/invest/mutual-funds"', 'href="/invest/bonds"',
               'href="/invest/commodities"', "foot-links"]:
    print(("OK  " if needle in nb2 else "MISS"), "nav:" + needle)
css = pathlib.Path("static/app.css").read_text(encoding="utf-8")
for needle in [".nav-group", ".nav-drop", ".nav-menu", ".crumbs", ".foot-links"]:
    print(("OK  " if needle in css else "MISS"), "css:" + needle)
js = pathlib.Path("static/app.js").read_text(encoding="utf-8")
for needle in ["nav-drop", "closeGroups", "crumbs"]:
    print(("OK  " if needle in js else "MISS"), "js:" + needle)

print("DONE")
