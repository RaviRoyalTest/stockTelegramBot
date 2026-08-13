"""Detailed command guide for /learn (pure content, no I/O).

The bot has 30+ commands, so this module explains every one of them in a way
that is easy to recollect: what it does, the syntax, copy-paste examples,
what the output means, and tips. `/learn` shows the topic index; `/learn
TOPIC` (e.g. 'stocks', 'schedule', 'alerts') walks through a group; `/learn
/COMMAND` gives the full walkthrough of one command; `/learn all` prints
everything.
"""
from __future__ import annotations

from difflib import get_close_matches

# ---------------------------------------------------------------------------
# Per-command deep dives
# ---------------------------------------------------------------------------

COMMAND_LEARN: dict = {
    "/corpactions": {
        "what": "Browse every corporate action on NSE + BSE: dividends, bonus, splits, rights and buybacks.",
        "syntax": "/corpactions [TYPE | SYMBOL | N | today]",
        "examples": [
            "/corpactions dividend  \u2192 only dividend announcements",
            "/corpactions bonus|split|rights|buyback  \u2192 one action type",
            "/corpactions increase  \u2192 bonus + split + rights together",
            "/corpactions today  \u2192 ex-dates due today",
            "/corpactions 7  \u2192 ex-dates within the next 7 days",
            "/corpactions RELIANCE  \u2192 full history for one symbol",
            "/corpactions TATA  \u2192 keyword search (company/subject)",
        ],
        "output": "A list of actions, each showing the company, subject (e.g. 'Dividend - Rs 5.25 Per Share'), the current price, and the ex-date / record date. Dividend, bonus, split, rights and buyback alerts have their own emoji.",
        "tips": [
            "The ex-date is the cut-off: buy before it to qualify for the benefit.",
            "Tap the \U0001F4B9 button next to any symbol to jump straight to its fundamentals.",
        ],
        "aliases": "/ca, /corporate-actions, /corp-actions, /actions",
    },
    "/exdates": {
        "what": "All corporate actions sorted by their ex-date window.",
        "syntax": "/exdates [today | N]",
        "examples": ["/exdates today", "/exdates 10  \u2192 next 10 days", "/exdates  \u2192 default 5-day window"],
        "output": "Every action whose ex-date falls inside the window, newest first - handy for a quick 'what's paying out this week' scan.",
        "tips": ["Combine with /corpactionsformylist when you only care about YOUR stocks."],
        "aliases": "/exdate, /ex-dates",
    },
    "/corpactionssummary": {
        "what": "A snapshot of the market: how many actions per exchange and per type, plus the next ex-dates.",
        "syntax": "/corpactionssummary",
        "examples": [],
        "output": "Counts by exchange (NSE vs BSE) and by type (dividend/bonus/split/rights/buyback), then a list of the soonest ex-dates.",
        "tips": ["Great first thing in the morning to see the day's corporate-action calendar."],
        "aliases": "/summary, /casummary",
    },
    "/corpactionsformylist": {
        "what": "Corporate actions for YOUR watchlist only: upcoming ex-dates plus recently passed / in-progress actions with a status.",
        "syntax": "/corpactionsformylist",
        "examples": [],
        "output": "Three groups: \U0001F4C5 upcoming ex-dates, \U0001F4E2 announced-but-no-ex-date-yet, and \U0001F504 recently passed / in progress (last 30 days). Each row shows a status like 'Payment due by 11-Sep-2026', 'Rights window open' or 'Bonus credit'. This is the report the daily alert mirrors.",
        "tips": [
            "Keep this list current - /addstock RELIANCE NSE and it appears here too.",
            "Run it whenever you see an alert you want to double-check.",
        ],
        "aliases": "/next, /upcoming",
    },
    "/watchlist": {
        "what": "Show your full watchlist with live prices.",
        "syntax": "/watchlist",
        "examples": [],
        "output": "Numbered list: symbol (exchange), company name and current price with the day's move. Every alert, favourites bundle and list-wide report uses this list.",
        "tips": ["Your list is per-user and persists across redeploys (pushed to GitHub)."],
        "aliases": "/list",
    },
    "/addstock": {
        "what": "Add a stock to your watchlist.",
        "syntax": "/addstock SYMBOL [NSE|BSE]",
        "examples": ["/addstock RELIANCE NSE", "/addstock PGINVIT  \u2192 defaults to NSE", "/addstock DIXON BSE"],
        "output": "Confirmation with the company name. A typo shows similar NSE tickers to pick from instead of failing.",
        "tips": ["You can also paste a company name - the fuzzy search finds the symbol."],
        "aliases": "/add",
    },
    "/removestock": {
        "what": "Remove a stock from your watchlist.",
        "syntax": "/removestock SYMBOL",
        "examples": ["/removestock TCS"],
        "output": "Confirmation that the symbol is gone; the next alert/round-up skips it.",
        "aliases": "/remove",
    },
    "/myfavourites": {
        "what": "One command that runs all your regular reports together (corporate actions for your list, top losers 1h + today, watchlist, deep fundamentals).",
        "syntax": "/myfavourites [run | set CMD | add CMD | remove N | reset]",
        "examples": [
            "/myfavourites  \u2192 see what it runs",
            "/myfavourites run  \u2192 fire them all now",
            "/myfavourites add /scan500  \u2192 add a command to the bundle",
            "/myfavourites remove 3  \u2192 drop the 3rd command",
            "/myfavourites reset  \u2192 restore the default list",
        ],
        "output": "Your custom bundle of commands executed in one go - the whole market + your stocks in a single message set.",
        "tips": ["Think of it as a personal 'daily brief' button."],
        "aliases": "/favorites, /favourites, /mypicks, /dailybrief",
    },
    "/news": {
        "what": "Latest headlines for your watchlist stocks (or one symbol).",
        "syntax": "/news [N | SYMBOL]",
        "examples": ["/news  \u2192 news for all watchlist stocks", "/news 5  \u2192 5 headlines per stock", "/news RELIANCE  \u2192 RELIANCE only"],
        "output": "Headlines with the publisher and timestamp; each opens in your browser when tapped.",
        "tips": ["Watch the 52-week context from /fundamentalanalyze alongside the news for the full picture."],
    },
    "/fundamentalanalyze": {
        "what": "The quick analysis card for one stock (or your watchlist).",
        "syntax": "/fundamentalanalyze SYMBOL | mylist | N | N-M",
        "examples": [
            "/fundamentalanalyze TATATECH",
            "/fundamentalanalyze mylist  \u2192 whole watchlist, 10 per page",
            "/fundamentalanalyze 5-10  \u2192 watchlist positions #5-#10",
        ],
        "output": "Price + day move, 52-week signal, RSI, MACD, SMA 50/200, P/E, sector P/E, market cap, D/E, div yield, current ratio, book value, EPS, net margin, cash & debt, ROCE/ROE, shareholding, plus the analyst forecast (consensus, target, upside) and the top executive.",
        "tips": [
            "Type the symbol in the pick-list - similar tickers appear as you type.",
            "For the deep version use /fundamentalreport.",
        ],
        "aliases": "/stock, /stockanalysis, /analysis, /info, /quote, /fundamental-analysis",
    },
    "/fundamentalreport": {
        "what": "The DEEP fundamental report - every section of the quick card plus growth & margins, per-share scale, balance sheet & cash flow, technical indicators, screener.in annuals/quarters, analyst forecast, executives and competitors.",
        "syntax": "/fundamentalreport SYMBOL | mylist | N | N-M",
        "examples": [
            "/fundamentalreport RELIANCE",
            "/fundamentalreport 3-5  \u2192 watchlist #3..#5 (5 per page)",
            "/fundamentalreport mylist",
        ],
        "output": "Multi-section report: \U0001F4B0 price & movement, \U0001F4C8 technical indicators (RSI, MACD, SMA), \U0001F3F7\ufe0f valuation, \U0001F4C8 growth & margins, \U0001F4BC per-share, \U0001F4C9 balance sheet & cash flow, \U0001F3AF returns, screener.in annual + quarterly tables, \U0001F52D analyst view & forecast, \U0001F464 top executives, \U0001F3E2 top competitors (NSE).",
        "tips": ["Run /fundamentalreport before /scan500 picks for the fundamentals behind the technicals."],
        "aliases": "/fund, /fundamentals",
    },
    "/checklist": {
        "what": "A 32-point investment scorecard for one stock: 10 personal + 22 AI criteria checked and scored.",
        "syntax": "/checklist SYMBOL | mylist | N-M",
        "examples": ["/checklist RELIANCE", "/checklist mylist", "/checklist 5-10"],
        "output": "A scored breakdown (valuation, growth, profitability, safety, momentum...) with pass/fail per criterion and a final verdict.",
        "tips": ["Use it as a second opinion before acting on a /scan500 pick."],
        "aliases": "/investcheck, /scorecard, /qualitycheck, /quality",
    },
    "/usstock": {
        "what": "US stock details - live price + deep fundamentals in USD for any NASDAQ/NYSE ticker.",
        "syntax": "/usstock TICKER",
        "examples": ["/usstock AAPL", "/usstock NVDA", "/usstock BRK-B  \u2192 dot-tickers work", "/usstock MSFT"],
        "output": "Price & change, 52-week range, RSI/MACD/SMA technicals, P/E (trailing + forward), PEG, P/B, P/S, div yield, beta, market cap in $B, EV, growth & margins, EPS, cash/debt, D/E, FCF, ROE/ROCE, analyst targets, executives. Unknown tickers show similar US tickers to pick from.",
        "tips": [
            "Schedule it: /schedule add 3h /usstock AAPL us (runs only during US market hours).",
            "Works in the same reports pipeline as Indian stocks - just with $.",
        ],
        "aliases": "/usfund, /usquote, /us",
    },
    "/forecast": {
        "what": "The forecast value for a stock: analyst consensus & rating breakdown, the 12-month target price with upside, the top executives, and (NSE stocks) the top competitors by market cap.",
        "syntax": "/forecast SYMBOL",
        "examples": ["/forecast RELIANCE", "/forecast AAPL  \u2192 US works too"],
        "output": "\U0001F52D analyst analysis & forecast (consensus, Strong Buy/Buy/Hold/Sell counts, target price + upside, high/low range), \U0001F464 top executives with titles, \U0001F3E2 top competitors with CMP / market cap / P/E / ROCE (NSE).",
        "tips": ["The target price + upside is the quickest 'where is this stock heading' answer."],
        "aliases": "/analyst, /forecastanalysis",
    },
    "/indicator": {
        "what": "A clear deep-dive into ONE technical indicator for a stock - the value, the signal, the trend and how to read the levels.",
        "syntax": "/indicator SYMBOL [INDICATOR]",
        "examples": [
            "/indicator RELIANCE RSI",
            "/indicator AAPL MACD  \u2192 US works too",
            "/indicator RELIANCE  \u2192 the full all-indicators card",
        ],
        "output": "For a named indicator: current value(s) with a \U0001F7E2/\U0001F534 signal, the indicator's 5-session trend, a plain-language 'what it means' paragraph and a reading-levels legend. With no indicator name: the full /scan500-style card (score, all indicators, trade plan).",
        "tips": ["Indicators: rsi, macd, stochastic, bollinger, cci, adx, aroon, psar, supertrend, ema/sma, gmma, vwap, atr, donchian, squeeze, cmf, mfi, obv."],
        "aliases": "/ind, /tech, /technical",
    },
    "/harmonicpatterns": {
        "what": "Scan for harmonic patterns (Gartley, Bat, Butterfly, Crab, Shark) and get PRZ entry reports.",
        "syntax": "/harmonicpatterns [all | 100 | 500] [TIMEFRAME] | SYMBOL",
        "examples": [
            "/harmonicpatterns all  \u2192 NIFTY 100, daily",
            "/harmonicpatterns 500 1w  \u2192 NIFTY 500, weekly",
            "/harmonicpatterns RELIANCE  \u2192 full report for one stock",
        ],
        "output": "Detected patterns with the Potential Reversal Zone (PRZ), entry, stop-loss and targets, RSI context and a note per pattern.",
        "tips": ["Timeframes: 5m 15m 30m 1h 4h 1d 1w."],
        "aliases": "/harmonic",
    },
    "/scan500": {
        "what": "The full NIFTY 500 technical scanner: 30+ indicators over every stock, strict rejection rules, a /100 score, and a full indicator card for the TOP 10.",
        "syntax": "/scan500",
        "examples": [],
        "output": "Market regime & breadth, the #1 top trade setup (entry/SL/targets), a compact qualified table, then a \U0001F3C6 TOP 10 \u2014 FULL INDICATOR DETAIL card per stock: trend & structure (EMAs, SMA cross, Supertrend, GMMA, Donchian, VWAP, PSAR), momentum (RSI, MACD, ADX, Aroon, Stochastic, Bollinger, CCI), volume & flow (CMF, MFI, OBV, volume ratio, ADTV, delivery), and the trade plan (entry/SL/T1-3/R:R/ATR).",
        "tips": [
            "Takes ~1-2 minutes - it scans all ~500 stocks.",
            "Cross-check picks with /fundamentalreport and /forecast.",
            "Schedule it: /schedule add 3h /scan500 runs it automatically in market hours.",
        ],
    },
    "/topmovers": {
        "what": "Top gainers AND losers in one screen.",
        "syntax": "/topmovers [period] [N] [100 | 500 | nasdaq100]",
        "examples": ["/topmovers  \u2192 last 1h, NIFTY 100", "/topmovers 2d 500  \u2192 2-day movers, NIFTY 500", "/topmovers today nasdaq100  \u2192 today's US movers"],
        "output": "Rows with symbol, price, % move and the day's range context; ends with a Get Fundamentals button per row.",
        "tips": ["Periods: 5m 15m 30m 1h 2h 4h today 1d 2d 5d 1w 2w 1mo 3mo 6mo 1y.", "Market-hours gated: live screens run during trading hours + 1h after close (IST for NIFTY, ET for US); a DATE query like /topmovers 12-08-2026 works any time."],
        "aliases": "/movers, /marketmovers",
    },
    "/bigmovers": {
        "what": "ALL stocks beyond a % session move in ONE list - the watcher threshold as a full report.",
        "syntax": "/bigmovers [%] [nifty100 | nifty500 | sp500 | nasdaq100]",
        "examples": ["/bigmovers  \u2192 every stock up/down \u2265 5% today, NIFTY 500", "/bigmovers 8 sp500  \u2192 S&P 500 stocks beyond \u00b18%"],
        "output": "Every stock whose session move (price vs previous close) crosses your % threshold, ranked by |move| - like the /watcher alerts but all in one report.",
        "tips": ["Market-hours gated: runs during trading hours + 1h after close (IST for NIFTY, ET for US) so stale moves are never shown."],
        "aliases": "/moverlist, /watcherlist",
    },
    "/topgainers": {
        "what": "Top rising stocks.",
        "syntax": "/topgainers [period] [N] [100 | 500 | nasdaq100]",
        "examples": ["/topgainers 1h 10", "/topgainers 1mo 20 500", "/topgainers today nasdaq100"],
        "output": "Best-performing stocks over the window with price, % and fundamentals button. A bare 100/500 is the top-N count; use nifty100/nifty500 for the index.",
        "tips": ["Market-hours gated: live screens run during trading hours + 1h after close; DATE queries work any time."],
        "aliases": "/gainers",
    },
    "/toplosers": {
        "what": "Top falling stocks.",
        "syntax": "/toplosers [period] [N] [100 | 500 | nasdaq100]",
        "examples": ["/toplosers  \u2192 today's top 30 losers", "/toplosers 1h 10", "/toplosers 1w nifty100"],
        "output": "Worst-performing stocks over the window - a quick 'what's bleeding today' check.",
        "tips": ["Market-hours gated: live screens run during trading hours + 1h after close; DATE queries work any time."],
        "aliases": "/losers",
    },
    "/alertfilters": {
        "what": "Choose which action types you receive alerts for.",
        "syntax": "/alertfilters TYPE,TYPE",
        "examples": ["/alertfilters dividend,bonus", "/alertfilters all  \u2192 reset to all types"],
        "output": "Confirmation of the active filter; /settings shows it too.",
        "aliases": "/filter, /actionfilters",
    },
    "/pricealert": {
        "what": "Alert when a stock moves \u00b1PCT% in a day.",
        "syntax": "/pricealert PCT | off",
        "examples": ["/pricealert 3  \u2192 alert on any \u00b13% daily move", "/pricealert off  \u2192 disable"],
        "output": "Confirmation of the new threshold; the poller then pings you on big moves.",
        "aliases": "/alert",
    },
    "/watcher": {
        "what": "Sudden-move alerts: watch a universe and alert when a stock crosses your % threshold in a session.",
        "syntax": "/watcher on | off | set N | universe U",
        "examples": ["/watcher on", "/watcher set 5", "/watcher universe nifty500  \u2192 nifty100 | nifty500 | mylist"],
        "output": "Status + live alert stream of big movers as they happen.",
        "aliases": "/bigmover, /moverwatch",
    },
    "/moversfund": {
        "what": "Choose whether movers reports fetch fundamentals automatically or behind a button.",
        "syntax": "/moversfund button | auto",
        "examples": ["/moversfund auto  \u2192 fundamentals with every movers report", "/moversfund button  \u2192 tap the button instead (default)"],
        "output": "Confirmation of the mode; the next movers report follows it.",
    },
    "/settings": {
        "what": "View your current configuration in one place.",
        "syntax": "/settings",
        "examples": [],
        "output": "Your watchlist size, action filters, price-alert threshold, watcher state, movers-fundamentals mode, market gate and schedule summary.",
        "tips": ["Use it after any /alertfilters or /pricealert change to confirm it stuck."],
    },
    "/schedule": {
        "what": "Automated reports - run any command on a timer, only during your chosen market hours, with pause support.",
        "syntax": "/schedule add INTERVAL CMD [in|us|any] [from HH:MM to HH:MM] | pause D | resume | run | remove N | clear",
        "examples": [
            "/schedule add 3h /scan500  \u2192 every 3 hours (Indian market hours by default)",
            "/schedule add 3h /usstock AAPL us  \u2192 US hours only",
            "/schedule add at 09:15 /toplosers 1h  \u2192 daily at 09:15 IST",
            "/schedule add at 09:15,15:30 /toplosers 1h  \u2192 daily at BOTH times (start + end results)",
            "/schedule add 3h /cmd in from 09:15 to 15:30  \u2192 custom window - fires at its start AND end",
            "/schedule pause 1d | 2d | 3d | 1w | 2w | 1mo  \u2192 pause (auto-resumes)",
            "/schedule resume  \u2192 resume early  \u00b7  /schedule run  \u2192 run all now",
        ],
        "output": "Your schedule list with the next-run time in IST/ET, the market gate and the paused-until stamp; each report lands in YOUR chat.",
        "tips": [
            "Intervals: 1h, 2h, 3h, 6h, 12h, 1d... and clock times with 'at HH:MM'.",
            "Several clock times work: 'at 09:15,15:30' = open + close reports every session.",
            "A run window (from 09:15 to 15:30) always fires at its start AND end, whatever the interval.",
            "Each user has their own schedule - yours never affects others.",
        ],
        "aliases": "/sched",
    },
    "/market": {
        "what": "Your default market-hours gate for scheduled reports.",
        "syntax": "/market in | us | any",
        "examples": ["/market in  \u2192 NSE/BSE 09:15-15:30 IST", "/market us  \u2192 NASDAQ/NYSE 09:30-16:00 ET", "/market any  \u2192 no gate"],
        "output": "Live market status (open/closed for IN and US) plus your current gate.",
    },
    "/schednow": {
        "what": "Run all your scheduled commands right now, ignoring the timer.",
        "syntax": "/schednow",
        "examples": [],
        "output": "Every command on your schedule executes immediately, in order.",
        "aliases": "/schedule run",
    },
    "/checknow": {
        "what": "Force-run an alert check now and re-send every match.",
        "syntax": "/checknow",
        "examples": [],
        "output": "A fresh alert pass: corporate actions, reminders and price moves re-evaluated immediately.",
    },
    "/status": {
        "what": "Where your data lives and whether it survives redeploys.",
        "syntax": "/status",
        "examples": [],
        "output": "Your personal setup (watchlist, schedule, settings, alerts) plus the GitHub push / persistence status of the state files.",
    },
    "/menu": {
        "what": "A one-tap button menu so you never type a command.",
        "syntax": "/menu [off]",
        "examples": ["/menu  \u2192 show the buttons", "/menu off  \u2192 hide them"],
        "output": "A persistent reply keyboard with the main commands; tap to run.",
        "aliases": "/quick, /shortcuts, /buttons",
    },
    "/all": {
        "what": "Every command in one copyable list, grouped by purpose.",
        "syntax": "/all",
        "examples": [],
        "output": "The complete command reference - tap any line to copy it, then send it.",
    },
    "/help": {
        "what": "The styled command guide with examples.",
        "syntax": "/help  (also /start)",
        "examples": [],
        "output": "Grouped command guide + a colour/signal legend and quick examples.",
        "aliases": "/start",
    },
    "/learn": {
        "what": "This guide - detailed explanations of every command so you can recollect any of them.",
        "syntax": "/learn [TOPIC | /COMMAND | all]",
        "examples": [
            "/learn  \u2192 the topic index",
            "/learn stocks  \u2192 fundamental analysis group",
            "/learn schedule  \u2192 automation group",
            "/learn /scan500  \u2192 full walkthrough of one command",
            "/learn all  \u2192 the entire guide",
        ],
        "output": "A structured walkthrough: what each command does, the syntax, examples, what the output means and tips.",
        "aliases": "/guide, /explain, /tutorial, /howto",
    },
}

# ---------------------------------------------------------------------------
# Topic groups (each references commands from COMMAND_LEARN)
# ---------------------------------------------------------------------------

TOPICS = [
    {
        "name": "corporate actions",
        "title": "\U0001F4C5 Corporate Actions (NSE + BSE)",
        "blurb": "Everything about dividends, bonus, splits, rights and buybacks - the core alert feed.",
        "commands": ["/corpactions", "/exdates", "/corpactionssummary", "/corpactionsformylist"],
    },
    {
        "name": "watchlist",
        "title": "\u2B50 Watchlist & Favourites",
        "blurb": "Your personal stock list - every alert and report is built around it.",
        "commands": ["/watchlist", "/addstock", "/removestock", "/myfavourites", "/news"],
    },
    {
        "name": "stocks",
        "title": "\U0001F50D Fundamental Analysis (Indian Stocks)",
        "blurb": "Quick card, deep report and the 32-point scorecard - fundamentals of any NSE/BSE stock.",
        "commands": ["/fundamentalanalyze", "/fundamentalreport", "/checklist"],
    },
    {
        "name": "us stocks",
        "title": "\U0001F1FA\U0001F1F8 US Stocks & Forecast",
        "blurb": "US tickers in USD, plus the analyst forecast bundle that works for both markets.",
        "commands": ["/usstock", "/forecast"],
    },
    {
        "name": "technicals",
        "title": "\U0001F4C8 Technicals, Indicators & Scans",
        "blurb": "Per-indicator deep dives, harmonic patterns and the full NIFTY 500 scanner.",
        "commands": ["/indicator", "/harmonicpatterns", "/scan500"],
    },
    {
        "name": "movers",
        "title": "\U0001F4C9 Market Screens",
        "blurb": "Who is moving - gainers, losers and the full movers screen over any window.",
        "commands": ["/topmovers", "/topgainers", "/toplosers"],
    },
    {
        "name": "alerts",
        "title": "\u2699\ufe0f Alerts & Personalisation",
        "blurb": "Filters, price alerts, the sudden-move watcher and your settings.",
        "commands": ["/alertfilters", "/pricealert", "/watcher", "/moversfund", "/settings"],
    },
    {
        "name": "automation",
        "title": "\U0001F6E0 Automation & Scheduling",
        "blurb": "Run any command on a timer, gate it to market hours, pause it, or fire it now.",
        "commands": ["/schedule", "/market", "/schednow", "/checknow"],
    },
    {
        "name": "system",
        "title": "\U0001F4CA System & Status",
        "blurb": "Where your data lives and whether it survives redeploys.",
        "commands": ["/status"],
    },
    {
        "name": "menus",
        "title": "\U0001F447 Menus & Shortcuts",
        "blurb": "One-tap buttons, the full command list, the help guide - and this learn guide.",
        "commands": ["/menu", "/all", "/help", "/learn"],
    },
]

_TOPIC_ALIASES = {
    "corp": "corporate actions", "corporate": "corporate actions", "actions": "corporate actions",
    "dividend": "corporate actions", "dividends": "corporate actions",
    "watchlist": "watchlist", "list": "watchlist", "favourites": "watchlist",
    "favorites": "watchlist", "stocks": "stocks", "stock": "stocks", "fund": "stocks",
    "fundamental": "stocks", "fundamentals": "stocks", "fundamental analysis": "stocks",
    "us": "us stocks", "us stock": "us stocks", "us stocks": "us stocks", "forecast": "us stocks",
    "analyst": "us stocks", "tech": "technicals", "technical": "technicals", "technicals": "technicals",
    "indicator": "technicals", "indicators": "technicals", "scanner": "technicals", "scan": "technicals",
    "scan500": "technicals", "harmonic": "technicals",
    "stochastic": "technicals", "stoch": "technicals", "macd": "technicals",
    "rsi": "technicals", "bollinger": "technicals", "crossover": "technicals", "movers": "movers", "gainers": "movers",
    "losers": "movers", "market": "movers", "screens": "movers", "alerts": "alerts", "alert": "alerts",
    "settings": "alerts", "watcher": "alerts", "automation": "automation", "scheduling": "automation",
    "schedule": "automation", "sched": "automation", "system": "system", "status": "system",
    "menus": "menus", "menu": "menus", "shortcuts": "menus", "help": "menus", "guide": "menus",
    "all": "menus", "learn": "menus",
}

_COMMAND_ALIASES = {
    "ca": "/corpactions", "corpactions": "/corpactions", "exdate": "/exdates",
    "exdates": "/exdates", "summary": "/corpactionssummary", "casummary": "/corpactionssummary",
    "corpactionssummary": "/corpactionssummary", "next": "/corpactionsformylist",
    "upcoming": "/corpactionsformylist", "corpactionsformylist": "/corpactionsformylist",
    "watchlist": "/watchlist", "list": "/watchlist", "addstock": "/addstock", "add": "/addstock",
    "removestock": "/removestock", "remove": "/removestock", "myfavourites": "/myfavourites",
    "favorites": "/myfavourites", "favourites": "/myfavourites", "mypicks": "/myfavourites",
    "dailybrief": "/myfavourites", "news": "/news",
    "fundamentalanalyze": "/fundamentalanalyze", "stock": "/fundamentalanalyze",
    "stockanalysis": "/fundamentalanalyze", "analysis": "/fundamentalanalyze",
    "info": "/fundamentalanalyze", "quote": "/fundamentalanalyze",
    "fundamentalreport": "/fundamentalreport", "fund": "/fundamentalreport",
    "fundamentals": "/fundamentalreport", "checklist": "/checklist", "investcheck": "/checklist",
    "scorecard": "/checklist", "qualitycheck": "/checklist",
    "usstock": "/usstock", "usfund": "/usstock", "usquote": "/usstock", "us": "/usstock",
    "forecast": "/forecast", "analyst": "/forecast", "forecastanalysis": "/forecast",
    "indicator": "/indicator", "ind": "/indicator", "tech": "/indicator",
    "technical": "/indicator", "technicals": "/indicator",
    "harmonicpatterns": "/harmonicpatterns", "harmonic": "/harmonicpatterns",
    "scan500": "/scan500", "scan": "/scan500", "scanner": "/scan500",
    "topmovers": "/topmovers", "movers": "/topmovers", "marketmovers": "/topmovers",
    "topgainers": "/topgainers", "gainers": "/topgainers",
    "toplosers": "/toplosers", "losers": "/toplosers",
    "alertfilters": "/alertfilters", "filter": "/alertfilters", "actionfilters": "/alertfilters",
    "pricealert": "/pricealert", "alert": "/pricealert",
    "watcher": "/watcher", "bigmover": "/watcher", "moverwatch": "/watcher",
    "moversfund": "/moversfund", "settings": "/settings",
    "schedule": "/schedule", "sched": "/schedule", "market": "/market",
    "schednow": "/schednow", "checknow": "/checknow", "status": "/status",
    "menu": "/menu", "quick": "/menu", "buttons": "/menu", "shortcuts": "/menu",
    "all": "/all", "help": "/help", "start": "/help", "learn": "/learn", "guide": "/learn",
}


def _normalise(text: str) -> str:
    """Lowercase, strip the leading slash and collapse punctuation/whitespace."""
    return "".join(char for char in (text or "").lower() if char.isalnum() or char.isspace()).strip()


def resolve_target(arg: str):
    """Map a /learn argument to ('topic', name) or ('command', /cmd) or None.

    A slash-prefixed argument is always a command reference (`/learn /scan500`
    walks through the /scan500 command). A bare word is resolved as a topic
    first (`/learn schedule` walks the whole automation group), then as a
    command name or alias (`/learn ind` -> the /indicator walkthrough), then
    by fuzzy near-match so typos like 'stocs' still land.
    """
    norm = _normalise(arg)
    if not norm:
        return None
    if arg.strip().startswith("/") and norm in _COMMAND_ALIASES:
        return ("command", _COMMAND_ALIASES[norm])
    if norm in _TOPIC_ALIASES:
        return ("topic", _TOPIC_ALIASES[norm])
    if norm in _COMMAND_ALIASES:
        return ("command", _COMMAND_ALIASES[norm])
    candidates = list(_TOPIC_ALIASES.keys()) + list(_COMMAND_ALIASES.keys())
    close = get_close_matches(norm, candidates, n=1, cutoff=0.6)
    if close:
        return resolve_target(close[0])
    return None


# ---------------------------------------------------------------------------
# Renderers (pure lines for Telegram HTML)
# ---------------------------------------------------------------------------

def _command_summary(command: str) -> list[str]:
    """Compact recollect block for a topic view."""
    info = COMMAND_LEARN[command]
    lines = [f"<b>{command}</b> \u2014 {info['what']}"]
    lines.append(f"  Syntax: <code>{info['syntax']}</code>")
    examples = info.get("examples") or []
    if examples:
        lines.append("  Examples:")
        lines.extend(f"    \u2022 {example}" for example in examples[:2])
    aliases = info.get("aliases")
    if aliases:
        lines.append(f"  Aliases: {aliases}")
    lines.append(f"  Tip: {info['tips'][0]}" if info.get("tips") else "")
    return [line for line in lines if line]


def learn_index_lines() -> list[str]:
    """The topic index shown by a bare /learn."""
    lines = [
        "\U0001F4DA <b>LEARN \u2014 how to use every command</b>",
        "This bot has 30+ commands. Pick a topic to walk through it, type "
        "<code>/learn /COMMAND</code> for the full walkthrough of one command, "
        "or <code>/learn all</code> for everything.",
        "",
        "<b>Topics</b>",
    ]
    for topic in TOPICS:
        lines.append(f"  \u2022 <code>{topic['name']}</code> \u2014 {topic['title']} "
                     f"({', '.join(topic['commands'])})")
    lines.append("")
    lines.append("Examples: <code>/learn stocks</code> \u00b7 <code>/learn schedule</code> "
                 "\u00b7 <code>/learn /scan500</code> \u00b7 <code>/learn all</code>")
    return lines


def learn_command_lines(command: str) -> list[str]:
    """The full walkthrough of one command."""
    info = COMMAND_LEARN[command]
    lines = [
        f"\U0001F4DA <b>{command}</b> \u2014 {info['what']}",
        "",
        f"Syntax: <code>{info['syntax']}</code>",
        "",
        "<b>Examples</b>",
    ]
    examples = info.get("examples") or ["(no arguments needed - just send the command)"]
    lines.extend(f"  \u2022 {example}" for example in examples)
    lines.append("")
    lines.append("<b>What the output means</b>")
    lines.append(info["output"])
    if info.get("tips"):
        lines.append("")
        lines.append("<b>Tips</b>")
        lines.extend(f"  \u2022 {tip}" for tip in info["tips"])
    if info.get("aliases"):
        lines.append("")
        lines.append(f"Aliases: {info['aliases']}")
    lines.append("")
    lines.append("\U0001F4A1 <i>/learn for the topic index \u00b7 /help for the quick "
                 "one-line guide \u00b7 /all for the full command list.</i>")
    return lines


def learn_topic_lines(topic_name: str) -> list[str]:
    """The detailed walkthrough of one topic group."""
    topic = next((t for t in TOPICS if t["name"] == topic_name), None)
    if not topic:
        return learn_index_lines()
    lines = [
        f"{topic['title']}",
        topic["blurb"],
        "",
        "Commands in this group:",
    ]
    for command in topic["commands"]:
        lines.extend(_command_summary(command))
        lines.append("")
    lines.append("\U0001F4A1 <i>Type <code>/learn /COMMAND</code> for the full "
                 "walkthrough of any command above (e.g. "
                 f"<code>/learn {topic['commands'][0]}</code>).</i>")
    return lines


def learn_all_lines() -> list[str]:
    """The entire guide: index + every topic."""
    lines = learn_index_lines()
    lines.append("")
    lines.append("=" * 30)
    lines.append("")
    for topic in TOPICS:
        lines.extend(learn_topic_lines(topic["name"]))
        lines.append("=" * 30)
        lines.append("")
    return lines
