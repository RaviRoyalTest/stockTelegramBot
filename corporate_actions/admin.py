"""Web-admin gate + operations (dashboard.py is the only intended caller).

Login model (deliberately simple, no new dependencies):

  ADMIN_KEY  env var (set on Render). When unset the whole admin area is
  disabled - every endpoint answers 403 and the page says so. The value is
  compared with secrets.compare_digest and never logged or echoed back.

  POST /admin/login with the key creates an opaque session token (secrets
  .token_urlsafe, server-side dict, 12h expiry). The browser stores it in
  localStorage and sends it as "X-Admin-Token" on every admin API call.
  Sessions live only in the serving process, so a redeploy logs the admin
  out - acceptable for a single-admin dashboard.

Operations here are thin wrappers over the storage layer so both the web
endpoints and unit tests exercise exactly what the Telegram bot uses.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time

from . import config, storage

log = logging.getLogger(__name__)

_SESSION_TTL_SECONDS = 12 * 3600
_sessions: dict[str, float] = {}  # token -> expiry epoch
_sessions_lock = threading.Lock()


def admin_key() -> str:
    """The configured admin key ('' = admin area disabled).

    Read at call time (not import time) so setting the env var on the host
    takes effect without a code change beyond the redeploy itself.
    """
    return os.getenv("ADMIN_KEY", "").strip()


def is_enabled() -> bool:
    """True when the admin area can be used on this host."""
    return bool(admin_key())


def create_session(password: str) -> str | None:
    """Exchange the admin key for a session token (None = wrong key/disabled)."""
    expected = admin_key()
    if not expected or not secrets.compare_digest(str(password or ""), expected):
        log.warning("admin login rejected")
        return None
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        # Opportunistic cleanup of expired sessions while we hold the lock.
        now = time.time()
        for stale in [t for t, exp in _sessions.items() if exp <= now]:
            _sessions.pop(stale, None)
        _sessions[token] = now + _SESSION_TTL_SECONDS
    log.info("admin session created (total: %d)", len(_sessions))
    return token


def verify_session(token: str) -> bool:
    """True when the token is a live admin session (constant-time compare)."""
    if not token:
        return False
    with _sessions_lock:
        expiry = None
        for known in _sessions:
            if secrets.compare_digest(token, known):
                expiry = _sessions[known]
                break
        if expiry is None:
            return False
        if expiry <= time.time():
            _sessions.pop(token, None)
            return False
        return True


def end_session(token: str) -> None:
    """Logout: drop one session (no-op when unknown)."""
    with _sessions_lock:
        for known in _sessions:
            if secrets.compare_digest(token, known):
                _sessions.pop(known, None)
                return


def check_token(token: str) -> str:
    """Return "" when the request may proceed, else the error reason."""
    if not is_enabled():
        return "admin area is disabled (set ADMIN_KEY on the host)"
    if not verify_session(token or ""):
        return "invalid or expired admin session"
    return ""


# ---------------------------------------------------------------- users ----


def list_users() -> dict:
    """Every chat the bot knows, with its list size, mail id and toggles."""
    users: dict[str, dict] = {}
    for chat_id, settings in storage.load_settings().items():
        users[str(chat_id)] = {
            "chat": str(chat_id),
            "is_owner": str(chat_id) == str(config.TELEGRAM_CHAT_ID or ""),
            "list_count": 0,
            "email": settings.get("email") or "",
            "daily_email": bool(settings.get("daily_email")),
            "ca_alerts": bool(settings.get("ca_alerts", True)),
            "price_alert_pct": settings.get("price_alert_pct"),
            "quiet": bool(settings.get("quiet")),
            "quiet_until": settings.get("quiet_until"),
        }
    for chat_id in storage.load_subscriptions():
        users.setdefault(str(chat_id), {
            "chat": str(chat_id),
            "is_owner": False,
            "list_count": 0,
            "email": "",
            "daily_email": False,
            "ca_alerts": True,
            "price_alert_pct": None,
            "quiet": False,
            "quiet_until": None,
        })
    for chat_id, items in storage.load_subscriptions().items():
        users[str(chat_id)]["list_count"] = len(items)
    owner = str(config.TELEGRAM_CHAT_ID or "")
    if owner:
        users.setdefault(owner, {
            "chat": owner,
            "is_owner": True,
            "list_count": 0,
            "email": "",
            "daily_email": False,
            "ca_alerts": True,
            "price_alert_pct": None,
            "quiet": False,
            "quiet_until": None,
        })
        users[owner]["list_count"] = len(storage.load_watchlist())
    return dict(sorted(users.items(), key=lambda kv: (not kv[1]["is_owner"], kv[0])))


def user_list(chat_id: str) -> list:
    """One chat's subscription list (owner's = the app watchlist)."""
    return storage.get_user_list(_clean_chat(chat_id))


def add_user_symbols(chat_id: str, items: list[dict]) -> dict:
    """Append validated items to a chat's list (deduped). Returns a summary."""
    return storage.bulk_add_to_user_list(_clean_chat(chat_id), items)


def remove_user_symbol(chat_id: str, symbol: str, exchange: str) -> list:
    """Drop one symbol from a chat's list. Returns the new list."""
    return storage.remove_from_user_list(
        _clean_chat(chat_id), str(symbol or ""), str(exchange or "NSE")
    )


# ------------------------------------------------------------- schedule ----


def user_schedule(chat_id: str) -> list[dict]:
    """The schedule entries that belong to one chat."""
    return storage.load_schedule_for(_clean_chat(chat_id))


# Commands worth scheduling (report producers - the web Add Entry dropdown
# and validation share this single list). Values are runnable examples.
_SCHEDULE_SUGGESTIONS = (
    ("/openreport", "Opening/closing screener, both markets"),
    ("/openreport in", "India screener only"),
    ("/openreport us", "US screener only"),
    ("/openmarket", "Recorded report, instant replay"),
    ("/topmovers", "Top gainers + losers"),
    ("/topgainers", "Top rising stocks"),
    ("/toplosers", "Top falling stocks"),
    ("/moversover", "All stocks over N% today"),
    ("/gappers", "Overnight gaps"),
    ("/scan500", "NIFTY 500 technical scanner"),
    ("/screen", "Fundamental screener"),
    ("/myfavourites", "Your favourites bundle"),
    ("/corpactionsformylist", "Corporate actions for your list"),
    ("/corpactionssummary", "Corporate-action snapshot"),
    ("/news", "Latest watchlist headlines"),
    ("/checklist", "Investment scorecard"),
    ("/snap", "Record the last session"),
    ("/emailboth", "Open + close + EOD in one mail"),
    ("/emailopen", "Opening screener mail"),
    ("/emailclose", "Closing + EOD mail"),
)


def schedule_command_suggestions() -> list[dict]:
    """Dropdown suggestions for schedule commands: [{value, hint}]."""
    return [{"value": value, "hint": hint} for value, hint in _SCHEDULE_SUGGESTIONS]


def add_schedule(chat_id: str, interval_min: int, commands: list[str],
                 run_at: str | None = None, market: str | None = None) -> list[dict]:
    """Add a scheduled report for a chat. Returns the chat's new schedule."""
    chat = _clean_chat(chat_id)
    interval = int(interval_min)
    if interval < 1:
        raise ValueError("interval_min must be >= 1")
    cleaned = [str(c).strip() for c in (commands or []) if str(c).strip()]
    cleaned = [c if c.startswith("/") else "/" + c for c in cleaned]
    if not cleaned:
        raise ValueError("at least one command is required")
    if run_at is not None and not str(run_at).strip():
        run_at = None
    storage.add_schedule_entry(
        interval, cleaned, chat,
        run_at=str(run_at).strip() if run_at else None,
        market=str(market).strip().lower() if market else None,
    )
    return storage.load_schedule_for(chat)


def remove_schedule(chat_id: str, index: int) -> list[dict]:
    """Remove the index-th (0-based) schedule row of one chat."""
    chat = _clean_chat(chat_id)
    storage.remove_schedule_entry(chat, int(index))
    return storage.load_schedule_for(chat)


def clear_schedule(chat_id: str) -> list[dict]:
    """Remove every schedule row of one chat."""
    chat = _clean_chat(chat_id)
    storage.clear_schedule(chat)
    return storage.load_schedule_for(chat)


def pause_schedule(chat_id: str, hours: float | None) -> list[dict]:
    """Pause a chat's schedule rows (None/<=0 = indefinite) until resumed."""
    chat = _clean_chat(chat_id)
    if hours is not None and float(hours) > 0:
        until = time.time() + float(hours) * 3600
    else:
        until = time.time() + 365 * 24 * 3600  # "indefinite" = 1 year out
    storage.pause_schedule(chat, until)
    return storage.load_schedule_for(chat)


def resume_schedule(chat_id: str) -> list[dict]:
    """Resume (unpause) every schedule row of one chat."""
    chat = _clean_chat(chat_id)
    storage.resume_schedule(chat)
    return storage.load_schedule_for(chat)


# -------------------------------------------------------------- toggles ----


def set_alerts_enabled(chat_id: str, enabled: bool) -> dict:
    """Master toggle for a chat's automatic pushes (/quiet equivalent).

    Admin "off" pauses ALL background pushes (corporate actions, reminders,
    price alerts, watcher, scheduled reports) while command replies keep
    working - exactly what the user's own /quiet does.
    """
    chat = _clean_chat(chat_id)
    settings = dict(storage.get_user_settings(chat) or {})
    if enabled:
        settings.pop("quiet", None)
        settings.pop("quiet_until", None)
    else:
        settings["quiet"] = True
        settings.pop("quiet_until", None)
    storage.save_user_settings(chat, settings)
    log.info("admin: chat %s alerts -> %s", chat, "on" if enabled else "off")
    return _user_state(chat)


def set_ca_alerts(chat_id: str, enabled: bool) -> dict:
    """Toggle only the corporate-action + reminder pushes for a chat."""
    chat = _clean_chat(chat_id)
    settings = dict(storage.get_user_settings(chat) or {})
    settings["ca_alerts"] = bool(enabled)
    storage.save_user_settings(chat, settings)
    log.info("admin: chat %s ca_alerts -> %s", chat, bool(enabled))
    return _user_state(chat)


def _user_state(chat: str) -> dict:
    """Fresh settings snapshot for the chat after a mutation."""
    settings = storage.get_user_settings(chat) or {}
    return {
        "chat": chat,
        "quiet": bool(settings.get("quiet")),
        "quiet_until": settings.get("quiet_until"),
        "ca_alerts": bool(settings.get("ca_alerts", True)),
        "daily_email": bool(settings.get("daily_email")),
        "email": settings.get("email") or "",
    }


def _clean_chat(chat_id) -> str:
    """Normalize a chat id ('862087765' / '@StockVigilBot'); reject junk."""
    chat = str(chat_id or "").strip()
    if not chat:
        raise ValueError("chat id is required")
    if not (chat.lstrip("-").isdigit() or chat.startswith("@")):
        raise ValueError(f"invalid chat id: {chat}")
    return chat
