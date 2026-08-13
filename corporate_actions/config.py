"""Central configuration loaded from environment / .env file."""
import logging
import os
from datetime import date, datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv optional at import time
    pass

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Bot username (without the @) used to build blue one-tap command buttons
# (https://t.me/<username>?text=/cmd ...) that pre-fill a command into the
# user's input box. Override with BOT_USERNAME; defaults to the bot's name.
BOT_USERNAME = os.getenv("BOT_USERNAME", "StockVigilBot").strip().lstrip("@")


def _env_int(name: str, default: int, floor: int | None = None) -> int:
    """Parse an int env var defensively.

    A malformed value must not crash the whole app at import time, so we fall
    back to the default and log a warning instead of raising ValueError.
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("Invalid integer for %s=%r - using default %d", name, raw, default)
        return default
    if floor is not None and value < floor:
        log.warning("%s=%d is below minimum %d - using %d", name, value, floor, floor)
        return floor
    return value


POLL_INTERVAL_SECONDS = _env_int("POLL_INTERVAL_SECONDS", 3600, floor=60)
# How often the sudden-move watcher scans its universes for big session moves
# (see /watcher in Telegram and the System tab in the web dashboard).
MOVERS_WATCH_INTERVAL_SECONDS = _env_int("MOVERS_WATCH_INTERVAL_SECONDS", 180, floor=60)
LOOKBACK_DAYS = _env_int("LOOKBACK_DAYS", 30, floor=1)
# How many days ahead of the ex-date a reminder is sent (0 disables reminders).
REMINDER_DAYS = _env_int("REMINDER_DAYS", 5, floor=0)
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 20, floor=5)
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

# Scheduled reports: run a set of commands to the owner chat on a timer so
# fresh screens (e.g. "/topmovers 30m" and "/scan500") arrive without anyone
# typing them. Only the always-on server (PROCESS_COMMANDS=true) runs these;
# the GitHub Actions cron skips them so two processes never send duplicates.
SCHEDULED_REPORTS_ENABLED = (
    os.getenv("SCHEDULED_REPORTS_ENABLED", "true").strip().lower()
    in ("1", "true", "yes", "on")
)
SCHEDULED_REPORTS_INTERVAL_MIN = _env_int("SCHEDULED_REPORTS_INTERVAL_MIN", 180, floor=15)
SCHEDULED_REPORTS_CHAT = os.getenv("SCHEDULED_REPORTS_CHAT", "").strip() or TELEGRAM_CHAT_ID
SCHEDULED_COMMANDS = [
    item.strip() for item in os.getenv(
        "SCHEDULED_COMMANDS", "/scan500"
    ).split(",")
    if item.strip()
]
# Default market-hours gate for scheduled reports: 'in' (India NSE/BSE
# 09:15-15:30 IST), 'us' (NASDAQ/NYSE 09:30-16:00 ET) or 'any' (no gating).
# Per-entry overrides can be set with /schedule add ... us|any. When a gate
# is active an automatic report only fires while that market is open.
SCHEDULED_REPORTS_MARKET = os.getenv("SCHEDULED_REPORTS_MARKET", "in").strip().lower()

WATCHLIST_FILE = Path(os.getenv("WATCHLIST_FILE", str(BASE_DIR / "watchlist.json")))
SUBSCRIPTIONS_FILE = Path(os.getenv("SUBSCRIPTIONS_FILE", str(BASE_DIR / "subscriptions.json")))
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", str(BASE_DIR / "settings.json")))
SEEN_FILE = Path(os.getenv("SEEN_FILE", str(BASE_DIR / "seen_actions.json")))
SCHEDULE_FILE = Path(os.getenv("SCHEDULE_FILE", str(BASE_DIR / "schedule.json")))

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


def today_ist() -> date:
    """Today's date in India Standard Time (Asia/Kolkata).

    The host often runs on UTC, where the date flips at 18:30 IST. Reminder
    windows, per-day alert dedupe keys and corporate-action lookback windows
    must follow the market's calendar, not the host's local date.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Kolkata")).date()
    except Exception:  # zoneinfo/tzdata unavailable - fall back to host local
        return date.today()


def redact(text) -> str:
    """Strip the bot token from any string before it reaches logs."""
    sanitized = str(text)
    if TELEGRAM_BOT_TOKEN:
        sanitized = sanitized.replace(TELEGRAM_BOT_TOKEN, "***")
    return sanitized
