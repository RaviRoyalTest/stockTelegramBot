"""Persistent storage for the selected watchlist and de-duplication cache."""
import json
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()


def _read_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data
    except (OSError, ValueError):
        pass
    return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------- watchlist

def load_watchlist() -> list[dict]:
    """Return list of {'symbol', 'company', 'exchange'}."""
    with _lock:
        data = _read_json(config.WATCHLIST_FILE, [])
    return [item for item in data if isinstance(item, dict)]


def save_watchlist(watchlist: list[dict]) -> None:
    with _lock:
        _write_json(config.WATCHLIST_FILE, watchlist)


def _watchlist_key(item: dict) -> tuple:
    return (item.get("exchange", "").upper(), item.get("symbol", "").upper())


def add_to_watchlist(items: list[dict]) -> list[dict]:
    """Add entries, de-duplicating on (exchange, symbol). Returns new list."""
    with _lock:
        current = _read_json(config.WATCHLIST_FILE, [])
    seen = {_watchlist_key(i) for i in current}
    for item in items:
        key = _watchlist_key(item)
        if key not in seen and item.get("symbol"):
            current.append(item)
            seen.add(key)
    with _lock:
        _write_json(config.WATCHLIST_FILE, current)
    return current


def remove_from_watchlist(symbol: str, exchange: str) -> list[dict]:
    with _lock:
        current = _read_json(config.WATCHLIST_FILE, [])
    kept = [
        i
        for i in current
        if not (i.get("symbol", "").upper() == symbol.upper()
                and i.get("exchange", "").upper() == exchange.upper())
    ]
    with _lock:
        _write_json(config.WATCHLIST_FILE, kept)
    return kept


# ------------------------------------------------------------------ users
# The owner's list is the app watchlist (watchlist.json). Other Telegram users
# each get their own subscription (subscriptions.json) and receive alerts in
# their own chat.

def is_owner(chat_id) -> bool:
    return str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def load_subscriptions() -> dict:
    """Return {chat_id(str): [item, ...]} for non-owner users."""
    with _lock:
        data = _read_json(config.SUBSCRIPTIONS_FILE, {})
    return {str(k): v for k, v in data.items() if isinstance(v, list)}


def get_user_list(chat_id) -> list:
    if is_owner(chat_id):
        return load_watchlist()
    subs = load_subscriptions()
    return subs.get(str(chat_id), [])


def add_to_user_list(chat_id, item: dict) -> list:
    if is_owner(chat_id):
        return add_to_watchlist([item])
    subs = load_subscriptions()
    key = str(chat_id)
    current = subs.get(key, [])
    seen = {_watchlist_key(i) for i in current}
    if item.get("symbol") and _watchlist_key(item) not in seen:
        current.append(item)
    subs[key] = current
    with _lock:
        _write_json(config.SUBSCRIPTIONS_FILE, subs)
    return current


def remove_from_user_list(chat_id, symbol: str, exchange: str) -> list:
    if is_owner(chat_id):
        return remove_from_watchlist(symbol, exchange)
    subs = load_subscriptions()
    key = str(chat_id)
    current = [
        i for i in subs.get(key, [])
        if not (i.get("symbol", "").upper() == symbol.upper()
                and i.get("exchange", "").upper() == exchange.upper())
    ]
    subs[key] = current
    with _lock:
        _write_json(config.SUBSCRIPTIONS_FILE, subs)
    return current


# ------------------------------------------------------------------ settings
# Per-user alert preferences (action-type filters, price-move threshold) live in
# settings.json so they survive restarts and GitHub Actions re-runs.


def load_settings() -> dict:
    """Return {chat_id(str): settings dict} for all users."""
    with _lock:
        data = _read_json(config.SETTINGS_FILE, {})
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def get_user_settings(chat_id) -> dict:
    """Return the settings dict for a chat (empty dict when none stored)."""
    return load_settings().get(str(chat_id), {})


def save_user_settings(chat_id, settings: dict) -> None:
    """Merge/replace a chat's settings dict."""
    with _lock:
        current = _read_json(config.SETTINGS_FILE, {})
    current[str(chat_id)] = settings
    with _lock:
        _write_json(config.SETTINGS_FILE, current)


# ---------------------------------------------------------------- seen cache

def load_seen() -> set:
    """Return set of event keys already notified (survives restarts)."""
    with _lock:
        data = _read_json(config.SEEN_FILE, [])
    return set(data) if isinstance(data, list) else set()


def save_seen(keys: set) -> None:
    with _lock:
        _write_json(config.SEEN_FILE, sorted(keys))
