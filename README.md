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
  (dividend / bonus / split / rights / buyback) via `/filter` or the web UI.
- **Price-move alerts** - get a Telegram alert when a watched stock moves
  beyond a threshold (e.g. ±3% in a day) via `/alert 3` or the web UI.
- `/next` bot command - instantly list upcoming ex-dates for your watchlist.
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
| `/add SYMBOL [NSE/BSE]` | `/add RELIANCE NSE` | Add a stock (validated via Yahoo) |
| `/remove SYMBOL [NSE/BSE]` | `/remove TCS` | Remove a stock |
| `/list` | `/list` | Show the current watchlist |
| `/next` | `/next` | List upcoming ex-dates (next `REMINDER_DAYS` days) |
| `/filter TYPE,...` | `/filter dividend,bonus` | Only receive these action types (`/filter all` resets) |
| `/alert PCT` | `/alert 3` | Alert on daily moves of ±PCT% (`/alert off` disables) |
| `/status` | `/status` | Show where your list is saved and whether GitHub push is configured |
| `/help` | `/help` | Show commands |

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

## Project layout

```
app.py                      # Streamlit UI
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
  command (`/add`, `/remove`, `/filter`, `/alert`) and by the workflow cron
  after every poll. Read-only commands (`/list`, `/status`, `/next`) never
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
- To mute one source entirely (e.g. run NSE-only), the poller only iterates
  the exchanges it knows about — edit `FETCHERS` in `corp_actions/poller.py`.
- Data comes from public NSE/BSE endpoints; use for informational purposes.
