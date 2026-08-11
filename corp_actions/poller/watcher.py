"""Sudden-move watcher: which users/universes to watch and what to alert.

Pure helpers - the Poller engine (engine.py) owns the thread loop and the
Telegram sends; this module only answers "who to watch" and "what moved".
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import storage
from ..sources import get_index_universe, get_quote


def watcher_targets() -> list[tuple[str, dict]]:
    """[(chat_id, watcher_settings)] for every chat with the watcher on."""
    out = []
    for chat_id, settings in storage.load_settings().items():
        w = settings.get("watcher") or {}
        if w.get("enabled") and float(w.get("threshold") or 0) > 0:
            out.append((str(chat_id), w))
    return out


def watcher_symbols(chat_id: str, universe: str) -> list[str]:
    """Resolve a watcher universe to symbols: nifty100 / nifty500 / mylist."""
    u = (universe or "nifty100").lower()
    if u in ("nifty500", "500", "all"):
        return get_index_universe("nifty500") or []
    if u in ("mylist", "watchlist"):
        items = storage.get_user_list(chat_id)
        return [i["symbol"] for i in items if isinstance(i, dict)]
    return get_index_universe("nifty100") or []


def unique_watch_pairs(targets) -> list[tuple[str, str]]:
    """[(chat_id, symbol)] covering every enabled user's universe, de-duplicated."""
    uniq: list[tuple[str, str]] = []
    seen_syms = set()
    for chat_id, w in targets:
        for sym in watcher_symbols(chat_id, w.get("universe", "nifty100")):
            key = (chat_id, sym.upper())
            if key not in seen_syms:
                seen_syms.add(key)
                uniq.append(key)
    return uniq


def fetch_quotes(uniq: list[tuple[str, str]]) -> dict[str, dict]:
    """{SYMBOL: quote} for the watch pairs, fetching in parallel."""
    quotes: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {
            ex.submit(get_quote, "NSE", sym): (chat_id, sym)
            for chat_id, sym in uniq
        }
        for fut in as_completed(futs):
            try:
                q = fut.result()
            except Exception:
                q = None
            if q and q.get("change_pct") is not None:
                quotes[futs[fut][1].upper()] = q
    return quotes


def pending_alerts(targets, quotes: dict[str, dict], seen: set, today) -> list[tuple[str, str, dict, float]]:
    """Alerts to send: (chat_id, symbol, quote, change_pct) not yet sent today."""
    out = []
    for chat_id, w in targets:
        threshold = float(w.get("threshold") or 0)
        for sym in watcher_symbols(chat_id, w.get("universe", "nifty100")):
            q = quotes.get(sym.upper())
            if not q or q.get("change_pct") is None:
                continue
            chg = float(q["change_pct"])
            if abs(chg) < threshold:
                continue
            key = f"mwatch|{chat_id}|{today.isoformat()}|{sym.upper()}"
            if key in seen:
                continue
            out.append((chat_id, sym, q, chg))
    return out
