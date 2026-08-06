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


# ---------------------------------------------------------------- seen cache

def load_seen() -> set:
    """Return set of event keys already notified (survives restarts)."""
    with _lock:
        data = _read_json(config.SEEN_FILE, [])
    return set(data) if isinstance(data, list) else set()


def save_seen(keys: set) -> None:
    with _lock:
        _write_json(config.SEEN_FILE, sorted(keys))
