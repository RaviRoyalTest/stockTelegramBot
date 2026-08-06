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
| `/help` | `/help` | Show commands |

Changes are committed to the repo automatically on the next run, so the
watchlist persists and survives restarts.

### Notes for GitHub Actions

- `watchlist.json` and `seen_actions.json` are tracked on purpose: the seen
  cache prevents re-sending the same alert every hour.
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

- NSE endpoints are open and tested. BSE's `api.bseindia.com` sits behind
  Cloudflare and commonly returns `403` from datacenter/VPN IPs; from a normal
  residential network it usually works. When blocked, the app warns and simply
  uses NSE data.
- To mute one source entirely (e.g. run NSE-only), the poller only iterates
  the exchanges it knows about — edit `FETCHERS` in `corp_actions/poller.py`.
- Data comes from public NSE/BSE endpoints; use for informational purposes.
