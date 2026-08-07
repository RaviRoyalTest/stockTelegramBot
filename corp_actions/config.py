"""Central configuration loaded from environment / .env file."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv optional at import time
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "3600"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))
# How many days ahead of the ex-date a reminder is sent (0 disables reminders).
REMINDER_DAYS = int(os.getenv("REMINDER_DAYS", "5"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
ENABLE_BSE = os.getenv("ENABLE_BSE", "true").strip().lower() in ("1", "true", "yes", "on")
# Only ONE process may poll Telegram's getUpdates for a bot token at a time.
# The always-on server (bot_server.py) handles commands; the GitHub Actions
# cron sets PROCESS_COMMANDS=false so two pollers never fight (double replies
# and HTTP 409 conflicts).
PROCESS_COMMANDS = (
    os.getenv("PROCESS_COMMANDS", "true").strip().lower()
    in ("1", "true", "yes", "on")
)
# When true, plain-text messages that mention corporate actions ("corporate
# action", "dividends", "shareholder increase", "ex-date today", ...) are
# answered with live query results, not just slash commands.
NATURAL_QUERIES = (
    os.getenv("NATURAL_QUERIES", "true").strip().lower()
    in ("1", "true", "yes", "on")
)

WATCHLIST_FILE = Path(os.getenv("WATCHLIST_FILE", str(BASE_DIR / "watchlist.json")))
SUBSCRIPTIONS_FILE = Path(os.getenv("SUBSCRIPTIONS_FILE", str(BASE_DIR / "subscriptions.json")))
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", str(BASE_DIR / "settings.json")))
SEEN_FILE = Path(os.getenv("SEEN_FILE", str(BASE_DIR / "seen_actions.json")))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

NSE_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"
NSE_STOCK_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

BSE_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=Main&Scripcode=&Debttype=Equity&industry=&segment=Equity&Status=Active"
)
BSE_ACTIONS_URL = "https://api.bseindia.com/BseIndiaAPI/api/CorpActionAnncmentW/w"


def redact(text) -> str:
    """Strip the bot token from any string before it reaches logs."""
    s = str(text)
    if TELEGRAM_BOT_TOKEN:
        s = s.replace(TELEGRAM_BOT_TOKEN, "***")
    return s
