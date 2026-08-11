"""Fetch corporate actions from every enabled source and match the watchlist."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from ..sources import get_bse_corporate_actions, get_nse_corporate_actions
from ..sources.errors import SourceError

log = logging.getLogger(__name__)

FETCHERS = {
    "NSE": get_nse_corporate_actions,
    "BSE": get_bse_corporate_actions,
}


def active_fetchers() -> dict:
    """Enabled sources (BSE optional via ENABLE_BSE)."""
    fetchers = dict(FETCHERS)
    if not config.ENABLE_BSE:
        fetchers.pop("BSE", None)
    return fetchers


def fetch_all_actions() -> tuple[list[dict], list[str], list[str]]:
    """Fetch corporate actions from all enabled sources.

    Returns (actions, errors, warnings). Source failures degrade gracefully:
    BSE is a warning, anything else is an error.
    """
    errors, warnings, all_actions = [], [], []
    for exchange, fetcher in active_fetchers().items():
        try:
            actions = fetcher()
            for action in actions:
                action["exchange"] = exchange
            all_actions.extend(actions)
        except SourceError as error:
            if exchange == "BSE":
                warnings.append(f"BSE unavailable (blocked by their WAF): {error}")
            else:
                errors.append(f"{exchange}: {error}")
        except Exception as error:  # keep the whole cycle alive on unexpected bugs
            log.exception("fetcher %s raised unexpectedly", exchange)
            errors.append(f"{exchange}: {error}")
    return all_actions, errors, warnings


def fetch_matching(watchlist: list[dict]) -> list[dict]:
    """Fetch corporate actions matching the watchlist.

    The unfiltered NSE feed only returns ~20 most-recent records, which
    usually misses most watchlist stocks. To get a complete picture we query
    the NSE API per-symbol for each watchlist stock (the API returns the full
    history for a given symbol). BSE is fetched once globally (when enabled).

    Never sends anything - used by the /next command and tests.
    """
    if not watchlist:
        return []

    # Group watchlist items by exchange
    nse_symbols = [
        watch_item["symbol"] for watch_item in watchlist
        if watch_item.get("exchange", "").upper() == "NSE"
    ]
    bse_symbols = [
        watch_item["symbol"] for watch_item in watchlist
        if watch_item.get("exchange", "").upper() == "BSE"
    ]

    all_actions: list[dict] = []

    # Query NSE per-symbol (parallel) to get full history for each watchlist stock
    if nse_symbols:
        def _fetch_nse(symbol):
            try:
                return get_nse_corporate_actions(symbol=symbol)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_nse, symbol): symbol for symbol in nse_symbols}
            for future in as_completed(futures):
                try:
                    all_actions.extend(future.result())
                except Exception:
                    pass

    # Query BSE globally (when enabled)
    if bse_symbols and config.ENABLE_BSE:
        try:
            bse_actions = get_bse_corporate_actions()
            all_actions.extend(bse_actions)
        except Exception:
            pass

    # Filter to only watchlist symbols
    wanted = {
        (watch_item.get("exchange", "").upper(), watch_item.get("symbol", "").upper())
        for watch_item in watchlist
    }
    return [
        action
        for action in all_actions
        if (action.get("exchange", "").upper(), action.get("symbol", "").upper()) in wanted
    ]
