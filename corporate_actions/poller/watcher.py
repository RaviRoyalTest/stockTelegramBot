"""Sudden-move watcher: which users/universes to watch and what to alert.

Pure helpers - the Poller engine (engine.py) owns the thread loop and the
Telegram sends; this module only answers "who to watch" and "what moved".
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import storage
from ..sources import get_index_universe, get_quote

# A chat that has never touched /watcher gets this default config - the
# watcher is ON by default at 5% over NIFTY 100 (same defaults /watcher on
# applies), so new users are covered without any setup.
DEFAULT_WATCHER = {"enabled": True, "threshold": 5.0, "universe": "nifty100"}


def watcher_settings(chat_id) -> dict:
    """The chat's effective watcher config (defaults when never configured)."""
    watcher = storage.get_user_settings(chat_id).get("watcher")
    if not watcher:
        return dict(DEFAULT_WATCHER)
    return watcher


def watcher_targets() -> list[tuple[str, dict]]:
    """[(chat_id, watcher_settings)] for every chat with the watcher on."""
    out = []
    for chat_id in storage.load_settings():
        watcher = watcher_settings(chat_id)
        if watcher.get("enabled") and float(watcher.get("threshold") or 0) > 0:
            out.append((str(chat_id), watcher))
    return out


def watcher_symbols(chat_id: str, universe: str) -> list[str]:
    """Resolve a watcher universe to symbols: nifty100 / nifty500 / mylist."""
    normalized_universe = (universe or "nifty100").lower()
    if normalized_universe in ("nifty500", "500", "all"):
        return get_index_universe("nifty500") or []
    if normalized_universe in ("mylist", "watchlist"):
        items = storage.get_user_list(chat_id)
        return [item["symbol"] for item in items if isinstance(item, dict)]
    return get_index_universe("nifty100") or []


def unique_watch_pairs(targets) -> list[tuple[str, str]]:
    """[(chat_id, symbol)] covering every enabled user's universe, de-duplicated."""
    unique_pairs: list[tuple[str, str]] = []
    seen_syms = set()
    for chat_id, watcher_settings in targets:
        for symbol in watcher_symbols(chat_id, watcher_settings.get("universe", "nifty100")):
            key = (chat_id, symbol.upper())
            if key not in seen_syms:
                seen_syms.add(key)
                unique_pairs.append(key)
    return unique_pairs


def fetch_quotes(unique_pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """{SYMBOL: quote} for the watch pairs, fetching in parallel."""
    quotes: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(get_quote, "NSE", symbol): (chat_id, symbol)
            for chat_id, symbol in unique_pairs
        }
        for future in as_completed(futures):
            try:
                quote = future.result()
            except Exception:
                quote = None
            if quote and quote.get("change_pct") is not None:
                quotes[futures[future][1].upper()] = quote
    return quotes


def pending_alerts(targets, quotes: dict[str, dict], seen: set, today) -> list[tuple[str, str, dict, float]]:
    """Alerts to send: (chat_id, symbol, quote, change_pct) not yet sent today."""
    out = []
    for chat_id, watcher_settings in targets:
        threshold = float(watcher_settings.get("threshold") or 0)
        for symbol in watcher_symbols(chat_id, watcher_settings.get("universe", "nifty100")):
            quote = quotes.get(symbol.upper())
            if not quote or quote.get("change_pct") is None:
                continue
            change = float(quote["change_pct"])
            if abs(change) < threshold:
                continue
            key = f"mwatch|{chat_id}|{today.isoformat()}|{symbol.upper()}"
            if key in seen:
                continue
            out.append((chat_id, symbol, quote, change))
    return out
