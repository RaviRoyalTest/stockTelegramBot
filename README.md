# Corporate Action Alerts (NSE & BSE) → Telegram

A Streamlit web app that lets you pick stocks (NSE + BSE) from a multi-select
dropdown, then continuously polls both exchanges for corporate actions
(dividends, splits, bonus issues, rights) and pushes matches to a Telegram bot.

## Features

- Multi-select dropdown to choose any number of stocks; deselect/remove anytime.
- Type-to-filter search across ~2400 NSE equities (plus BSE when reachable).
- Manual "Add symbol" for stocks not in the fetched list.
- Persistent watchlist (`watchlist.json`) — survives restarts.
- Background poller sends **new** corporate actions to Telegram (de-duplicated
  via `seen_actions.json` so nothing is re-sent across restarts).
- **Ex-date reminders** - warned once when an action's ex-date is `REMINDER_DAYS`
  (default 5) days away, so you're alerted before the event, not just at the
  announcement.
- **Action-type filters** - per-user, receive only the types you care about
  (dividend / bonus / split / rights / buyback) via `/alertfilters` (alias `/filter`) or the web UI.
- **Price-move alerts** - get a Telegram alert when a watched stock moves
  beyond a threshold (e.g. ±3% in a day) via `/pricealert 3` (alias `/alert`) or the web UI.
- `/corpactionsformylist` bot command (aliases `/upcoming`, `/next`) - instantly
  list upcoming ex-dates for your watchlist, plus recently passed /
  in-progress actions (e.g. a rights issue whose ex-date has just passed, or
  a dividend whose payment is still pending) with derived status.
- "Check now" button to force an immediate poll.
- Graceful handling when a source is unavailable (e.g. BSE is Cloudflare-blocked
  from datacenter IPs).

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure Telegram
cp .env.example .env
#   - TELEGRAM_BOT_TOKEN : create a bot via @BotFather and copy its token
#   - TELEGRAM_CHAT_ID  : your chat id via @userinfobot (or group id)

# 3. Run the app
streamlit run app.py
```

Open the URL shown in the terminal, then:

1. Click **Load stock list from NSE & BSE**.
2. Type/pick stocks in the multi-select dropdown (or add symbols manually).
3. Click **Start** in the sidebar to begin continuous polling.
4. Optionally hit **Send test message** to verify the bot works.

## Configuration (.env)

| Variable                | Default | Description                              |
| ----------------------- | ------- | ---------------------------------------- |
| `TELEGRAM_BOT_TOKEN`    | -       | Bot token from @BotFather (required)     |
| `TELEGRAM_CHAT_ID`      | -       | Chat/group id to receive alerts (required) |
| `POLL_INTERVAL_SECONDS` | `3600`  | Seconds between polls (>= ~300 to respect Telegram limits) |
| `LOOKBACK_DAYS`         | `30`    | Past-days window used for BSE fetch      |
| `REMINDER_DAYS`         | `5`     | Days ahead of ex-date to send a reminder (`0` = off) |
| `SCHEDULED_REPORTS_ENABLED` | `true` | Run scheduled screens to the owner chat on a timer (always-on server only) |
| `SCHEDULED_REPORTS_INTERVAL_MIN` | `180` | Minutes between scheduled reports (min 15) - used only when `schedule.json` has no entries |
| `SCHEDULED_REPORTS_CHAT` | owner | Chat id to send scheduled reports to (defaults to `TELEGRAM_CHAT_ID`) |
| `SCHEDULED_COMMANDS`     | `/scan500` | Comma-separated commands run on the schedule - used only when `schedule.json` has no entries |

## Deploy free on GitHub Actions (24/7 polling, no server)

The poller can run for free as a scheduled GitHub Actions workflow (hourly).
The stock list is managed **from Telegram** with bot commands; every run commits
the watchlist + de-dup state back to the repo.

### Setup

1. Create a GitHub repository (public or private) and push this project:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git add .
   git commit -m "corporate action alerts"
   git push -u origin master
   ```

2. Add secrets in **Settings → Secrets and variables → Actions**:
   - `TELEGRAM_BOT_TOKEN` — your bot token
   - `TELEGRAM_CHAT_ID` — your chat id
   (`GITHUB_TOKEN` is provided automatically; the workflow has
   `contents: write` so it can commit the watchlist back.)

3. The workflow `.github/workflows/poller.yml` runs every hour (or trigger it
   manually via the **Actions** tab → *corporate-action-poller* → Run workflow).

### Managing the stock list from Telegram

Send these to your bot:

| Command | Example | What it does |
| ------- | ------- | ------------ |
| `/corpactions [TYPE\|SYMBOL\|N\|today]` | `/corpactions increase` | Browse corporate actions across ALL NSE+BSE stocks, not just the watchlist. No arg = overview; `dividend`/`bonus`/`split`/`rights`/`buyback` filter by type; `increase` = shareholder increase (bonus + split + rights); `today`/`7` = ex-date window; a symbol (e.g. `RELIANCE`) = full detail; any other word = keyword search. Alias: `/ca` |
| `/exdates [today\|N]` | `/exdates 7` | All actions whose ex-date is today or within N days (default `REMINDER_DAYS`). Alias: `/exdate` |
| `/corpactionssummary` | `/corpactionssummary` | Corporate-action snapshot: counts by exchange and type, plus the next ex-dates. Alias: `/summary` |
| `/corpactionsformylist` | `/corpactionsformylist` | YOUR watchlist: upcoming ex-dates PLUS recently passed / in-progress actions with status (rights subscription open, dividend payment due/pending, bonus credit). Aliases: `/upcoming`, `/next` |
| `/myfavourites` | `/myfavourites` | Run your favourite commands in one go: corporate actions for your list, top losers (last 1h + today), your watchlist and deep fundamentals for your stocks. Aliases: `/favorites`, `/mypicks` |
| `/news [N\|SYMBOL]` | `/news 5` | Latest news headlines for the stocks in your list (up to N each, 1-5). `/news RELIANCE` = one symbol. Sources: Google News RSS, Yahoo fallback |
| `/topmovers [period] [direction] [N] [100\|500]` | `/topmovers 1h gainers 10 500` | Screen an index (NIFTY 100/500) by price movement over a window, sorted lower → higher. Periods: `5m 15m 30m 1h 2h 4h today 2d 1w 1mo` (default 1h). Direction: `gainers`/`losers`/`all`; `N` = max rows; a bare `100`/`500` picks the index (default 100). E.g. `/topmovers 1w 500`. Each row also shows P/E, sector P/E, 52-week high/low, dividend yield, promoter/FII/DII holding and debt/equity when available. Alias: `/movers` |
| `/topgainers [period] [N] [100\|500]` | `/topgainers 2d 100` | Top N gainers over an index (default NIFTY 500, top 30). Here a bare `100`/`500` means **top N** — to pick the index use `nifty100`/`nifty500` or a second number after a count (e.g. `/topgainers 1h 50 500`). Fundamentals shown per row. Alias: `/gainers` |
| `/toplosers [period] [N] [100\|500]` | `/toplosers 1mo 100` | Top N losers over an index (same options and fundamentals as `/topgainers`). E.g. `/toplosers 1w 20 500`, `/toplosers 30m 5 nifty100`. Alias: `/losers` |
| `/fundamentalanalyze SYMBOL` | `/fundamentalanalyze TATATECH` | Quick analysis card for one stock: price, P/E, 52-week signal, QoQ shareholding. Aliases: `/stock`, `/stockanalysis` |
| `/fundamentalanalyze N` / `N-M` / `mylist` | `/fundamentalanalyze 5-10` | Same analysis card for a range of watchlist positions (1-based). `/fundamentalanalyze 5` = first 5 stocks, `/fundamentalanalyze 5-10` = positions #5..#10, `mylist` = whole list (max 10 per query, with a Next button) |
| `/fundamentals SYMBOL` | `/fundamentals RELIANCE` | Deep fundamental report for one stock: valuation (P/E, fwd P/E, P/B, P/S, div yield), YoY growth & margins, EPS/book value, balance sheet, analyst targets, QoQ shareholding. Alias: `/fund` |
| `/fundamentals N` / `N-M` / `mylist` | `/fundamentals 3-5` | Deep report for a range of watchlist positions (same syntax as `/fundamentalanalyze`, max 5 per query; `mylist` = whole list) |
| `/harmonicpatterns [all\|100\|500] [TIMEFRAME]` | `/harmonicpatterns all` / `/harmonicpatterns 500` | Harmonic-pattern scan (alias `/harmonic`). With a universe keyword it scans the whole index (default NIFTY 100) and lists every stock showing a formation, sorted most actionable first (top 25). Each entry is clear and compact: line 1 = symbol, current price with +/-% move, pattern (Gartley/Bat/Butterfly/Crab/Shark), direction, status & signal; line 2 = the PRZ zone, the projected/completed D level and how far price is from it (e.g. `inside PRZ`, `2.8% below PRZ`). `/harmonicpatterns 500 1w` scans NIFTY 500 on the weekly chart. Without a universe keyword it stays the single-stock deep report: `/harmonicpatterns TATATECH 1h` (PRZ, Fibonacci ratios, entry/SL/targets, R:R, final signal) or `/harmonicpatterns 3` (watchlist position). Timeframes: `5m 15m 30m 1h 4h 1d 1w`. Not investment advice — entries wait for PRZ confirmation |
| `/scan500` | `/scan500` | Full NIFTY 500 multi-indicator CNC/MIS scanner. Computes EMAs (20/50/100/200), RSI, MACD, ADX, CMF, MFI, OBV, Aroon, TTM Squeeze, Donchian 52-week channel, weekly Supertrend, GMMA, anchored VWAP and Mansfield RS for all ~500 stocks, applies the strict "do not buy / do not show" rejection rules (weekly supertrend red, below 200 SMA, CMF < 0, MRS < 0, R:R < 1:2, SL > 8%, ADTV < ₹10cr), scores survivors /100 (≥75 qualifies) and reports: market regime + breadth, rejected stocks with reasons, #1 top trade setup with a 09:15–15:30 hourly execution roadmap, an approved-stocks matrix and a CNC vs MIS execution table. Takes ~1 minute. Delivery % is estimated from money-flow (real NSE delivery data isn't public via this feed) |
| `/schedule [add <interval> <cmd> \| remove <n> \| clear]` | `/schedule add 3h /scan500` | Owner-only: manage automated reports on the always-on server. `/schedule` lists the current schedule. `/schedule add <interval> <command>` runs a command on its own timer — interval is minutes (`180`), `m` (`90m`), `h` (`3h`) or `d` (`1d`), minimum 15 minutes, e.g. `/schedule add 3h /scan500`, `/schedule add 90m /topmovers 30m`. `/schedule remove <n>` deletes entry n (1-based as shown by `/schedule`), `/schedule clear` removes everything. Entries are saved to `schedule.json` and pushed to GitHub, so they survive redeploys. Without any file entries the `SCHEDULED_COMMANDS` env defaults are used. Alias: `/sched` |

For the universe token you can use the short forms too: `n100`/`nifty100` and `n500`/`nifty500` (e.g. `/topgainers n100` = today's top NIFTY 100 gainers, `/topmovers 1w n500`).

Every `/corpactions`, `/exdates` and `/summary` result includes the current price
(₹ with today's % change) and clearly printed Ex-date / Record date /
Announced date. `/corpactions SYMBOL` shows full detail (face value, series, ISIN)
plus a derived Status and book-closure dates.
| `/addstock SYMBOL [NSE/BSE]` | `/addstock RELIANCE NSE` | Add a stock (validated via Yahoo). Alias: `/add` |
| `/removestock SYMBOL [NSE/BSE]` | `/removestock TCS` | Remove a stock. Alias: `/remove` |
| `/watchlist` | `/watchlist` | Show the current watchlist. Alias: `/list` |
| `/alertfilters TYPE,...` | `/alertfilters dividend,bonus` | Only receive these action types (`/alertfilters all` resets). Alias: `/filter` |
| `/pricealert PCT` | `/pricealert 3` | Alert on daily moves of ±PCT% (`/pricealert off` disables). Alias: `/alert` |
| `/menu` | `/menu` | Show a one-tap button menu in Telegram — tap a button to run the command (no typing). `/menu off` hides it |
| `/settings` | `/settings` | Show your current filters, price-alert and list location |
| `/status` | `/status` | Show where your list is saved and whether GitHub push is configured |
| `/checknow` | `/checknow` | Force a check and re-send all matching alerts to your chat |
| `/help` | `/help` | Show commands |

You can also ask in plain text without a slash, e.g. "corporate action",
"shareholder increase", "dividends", or "ex-date today" — the bot answers
with the same live query results (toggle with `NATURAL_QUERIES=false`).

Changes are committed to the repo automatically on the next run, so the
watchlist persists and survives restarts.

### Notes for GitHub Actions

- `watchlist.json`, `seen_actions.json`, `subscriptions.json` and
  `settings.json` are tracked on purpose: the seen cache prevents re-sending
  the same alert every hour, and the settings file persists per-user filters
  and alert thresholds across runs.
- BSE will typically be 403-blocked (datacenter IPs); NSE + Yahoo prices work.
- Free tier: public repos get ~2000 minutes/month — one hourly run (~30s)
  uses only a few hours per month.

## Web Dashboard (dashboard.py)

A comprehensive, well-organised web dashboard that surfaces **all** bot
functionality in a browser — no Telegram needed for browsing:

| Tab | What it does |
| --- | ------------ |
| 📌 **Watchlist** | Load NSE/BSE stock lists, multi-select stocks, add/remove symbols manually, view live prices |
| 📋 **Corporate Actions** | Query live NSE+BSE actions by overview, ex-date window, action type, or symbol/keyword |
| 📊 **Market Screens** | Run movers / gainers / losers screens over NIFTY 100/500 with any period (5m → 1y) |
| 💹 **Stock Analysis** | Deep single-stock report: price, 52W signal, RSI, P/E, sector P/E, market cap, D/E, div yield, ROCE, ROE, QoQ shareholding |
| 📰 **News** | Latest headlines for your watchlist or a single symbol |
| 🎛️ **Alert Settings** | Action-type filters + price-move threshold (same as `/alertfilters` and `/pricealert`) |
| 🖥️ **System** | Poller status, config, subscribers, persistence / GitHub push status |

### Run on Render — just redeploy, nothing to change

`bot_server.py` now serves the dashboard **by default** (toggle with
`SERVE_DASHBOARD=false`). If your Render Web Service already runs
`python bot_server.py`, simply **push the new code and redeploy** — the
dashboard appears at your Render service URL while the Telegram bot keeps
running in the background (same `$PORT`, same health check).

### Run locally

```bash
streamlit run dashboard.py
```

## Always-on Telegram bot (bot_server.py)

For instant `/addstock`, `/removestock`, `/watchlist` and `/checknow` replies, run
`bot_server.py` on an always-on host such as a Render Web Service:

- **Service type:** Web Service (not Cron Job / Background Worker).
- **Start command:** `python bot_server.py`
- **Health Check Path:** `/` (default). Render marks a deploy complete only
  when the service answers HTTP on `$PORT` - the long-polling loop alone is
  not enough, so `bot_server.py` starts a tiny health server on `$PORT`
  before polling. If a deploy times out while the logs show `Starting
  long-polling bot (instant responses)...`, look for `Health server
  listening on http://0.0.0.0:<PORT>/` just above that line; if it is
  missing, the deployed code predates the health-server commit (commit
  `b7231ef` adds it) or the port is taken. If instead you see
  `DEPLOYMENT WILL TIMEOUT: could not bind health server on port...`,
  another process holds `$PORT` - free it and redeploy.
- **Env vars:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and to survive
  redeploys also `GH_TOKEN` + `GITHUB_REPOSITORY` (see Notes below).
- Keep the GitHub Actions cron's `PROCESS_COMMANDS=false` so only this
  server polls Telegram commands (avoids 409 conflicts / double replies).

## "My stocks vanish on redeploy" - how persistence works

Render's disk is **ephemeral**: anything written only to the server's
filesystem is wiped on the next deployment/restart. Your watchlist survives
only because the bot pushes the JSON state files back to the GitHub repo,
which is exactly what the next deploy is built from.

To make `/addstock` stick across redeploys:

1. **Deploy the latest code.** The startup log prints
   `Deployed commit <sha>` - if it is older than the current `main`, redeploy.
2. **Set two environment variables** on the Render service:
   - `GH_TOKEN` - a fine-grained personal access token for the repo with
     **Contents: Read and write** (GitHub → Settings → Developer settings →
     Fine-grained tokens), or a classic token with `repo` scope.
   - `GITHUB_REPOSITORY` - e.g. `RaviRoyalTest/stockTelegramBot`.
   Then redeploy.
3. **Verify** - on Render's Shell tab run:
   ```bash
   python run_bot.py --check
   ```
   A green verdict means `/addstock`, `/removestock`, `/alertfilters` and
   `/pricealert` changes will reach GitHub and survive redeploys. You can
   also send `/status` to the bot in Telegram.

The always-on server pushes state to GitHub right after every write command
**and** re-checks every `PUSH_FLUSH_SECONDS` (default 180) for anything left
unpushed (e.g. after a transient push failure), so a single failed push
self-heals instead of silently losing your stock on the next redeploy. The
GitHub Actions cron is a second safety net that commits state every hour.

> Paid alternative: mount a Render **Persistent Disk** and point
> `WATCHLIST_FILE`/`SUBSCRIPTIONS_FILE`/`SETTINGS_FILE`/`SEEN_FILE` at it
> (env vars) - then state lives on the disk and does not need GitHub.

## Project layout

```
app.py                      # Streamlit UI (original)
dashboard.py                # Comprehensive web dashboard (all features)
dashboard_server.py         # Render entry: Telegram bot + dashboard together
run_bot.py                  # GitHub Actions entry: Telegram commands + poll
corp_actions/
  config.py                 # env + endpoint configuration
  sources.py                # NSE & BSE stock lists and corporate actions
  storage.py                # watchlist + seen-cache persistence
  notifier.py               # Telegram send + message formatting
  poller.py                 # background polling loop
.github/workflows/poller.yml  # hourly cron workflow
```

## Notes

- **Only one process may poll the bot.** Telegram allows a single `getUpdates`
  consumer per token. If you run `bot_server.py` on an always-on host (e.g.
  Render) *and* the GitHub Actions cron both handle commands, you get double
  replies and `409 Conflict` errors in the logs. The workflow sets
  `PROCESS_COMMANDS=false` so it only polls alerts; the always-on server is
  the sole command responder. Never run two `bot_server.py` processes.
- **Where the watchlist lives.** The repo's `watchlist.json` /
  `subscriptions.json` / `settings.json` / `seen_actions.json` are the source
  of truth, committed and pushed by the always-on server after every WRITE
  command (`/addstock`, `/removestock`, `/alertfilters`, `/pricealert`, `/schedule`
  and their short aliases) and by the workflow cron after every poll.
  Read-only commands (`/watchlist`, `/status`, `/upcoming`, `/help`) never
  push or reset. Always-on hosts like Render have **ephemeral disks** -
  anything written but not pushed is wiped on redeploy. To persist changes
  from Render, set these env vars:
  - `GH_TOKEN` - a fine-grained PAT (repo → Contents: Read and write)
  - `GITHUB_REPOSITORY` - e.g. `RaviRoyalTest/stockTelegramBot`
  `bot_server.py` warns loudly at startup if they are missing, syncs the
  latest state from GitHub on boot, and pushes after each write command.
  Run `/status` in Telegram to confirm your chat's list location
  (`watchlist.json` for the owner, `subscriptions.json` for other users) and
  whether GitHub push is configured.
- NSE endpoints are open and tested. BSE's `api.bseindia.com` sits behind
  Cloudflare and commonly returns `403` from datacenter/VPN IPs; from a normal
  residential network it usually works. When blocked, the app warns and simply
  uses NSE data.
- `/topmovers`, `/topgainers` and `/toplosers` (aliases `/movers`, `/gainers`,
  `/losers`) show fundamentals per stock: P/E and
  sector P/E, 52-week high/low, dividend yield, promoter / FII / DII holding
  and debt-to-equity. Fundamentals come from Yahoo Finance (price, 52w range,
  P/E, dividend, D/E) plus screener.in (sector P/E, holdings), are cached for
  24h, and are shown best-effort — if a source is down or rate-limited, the
  affected fields are simply omitted. To protect against screener.in's
  aggressive rate limiting, its enrichment is paced (a few requests/second)
  and capped at the first 40 listed stocks.
- The movement screens reply in two stages so you are never left waiting
  blind: an immediate acknowledgment, then the initial report (movers +
  current price) as soon as quotes are in, and finally an updated full
  report with the fundamentals. The deployed bot logs every stage in
  realtime (ack, universe load, change-fetch progress, initial/final report,
  per-row failures, timings) so you can watch what the pipeline is doing.
- To mute one source entirely (e.g. run NSE-only), the poller only iterates
  the exchanges it knows about — edit `FETCHERS` in `corp_actions/poller.py`.
- Data comes from public NSE/BSE endpoints; use for informational purposes.
