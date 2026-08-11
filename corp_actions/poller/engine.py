"""Background poller: fetches corporate actions and pushes new ones to Telegram.

The poller runs in a daemon thread, reads the persisted watchlist every cycle,
and keeps a status dict that the Streamlit UI can display.

Beyond new-action alerts it also supports:
  * ex-date reminders (warn N days before the ex-date, once per action)
  * price-move alerts (notify when a watched stock moves beyond a threshold)
  * per-user action-type filters (dividend/bonus/split/rights/buyback only)
  * the sudden-move watcher (own faster cadence, see watcher.py)
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .. import config, storage
from ..core.dates import today_ist
from ..formatting import (
    format_corporate_action,
    format_mover_alert,
    format_price_alert,
    format_reminder,
)
from ..sources import get_quote
from ..sources.types import ACTION_TYPES, action_type
from ..telegram.client import NotifierError, send_message
from ..telegram.markup import symbol_buttons
from . import watcher as watcher_mod
from .events import (
    event_key,
    parse_ex_date,
    recently_passed,
    within_reminder_window,
)
from .fetchers import active_fetchers

log = logging.getLogger(__name__)

# Symbols whose per-symbol NSE corporate-action fetch failed recently. A
# delisted / renamed / non-equity symbol (ETF, gold bond, InvIT) is NOT in
# NSE's corporate-action feed, so the per-symbol API errors on it every
# cycle. Caching the miss stops the same symbol from failing + being counted
# as a poll error again and again (noisy "N error(s)" lines and wasted calls).
_nse_fetch_fail: dict[str, float] = {}
_NSE_FETCH_FAIL_TTL = 3600  # seconds - re-check the symbol hourly


class Poller:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._watcher_thread = None
        self.status = {
            "running": False,
            "last_run": None,
            "last_message": None,
            "last_error": None,
            "warnings": [],
            "total_sent": 0,
            "cycle": 0,
        }
        self._status_lock = threading.Lock()
        self._seen = storage.load_seen()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # Sudden-move watcher runs on its own faster cadence when enabled
        self._watcher_thread = threading.Thread(target=self._watcher_loop, daemon=True)
        self._watcher_thread.start()
        self._set("running", True)
        self._set("last_message", "Poller started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._watcher_thread:
            self._watcher_thread.join(timeout=5)
        self._set("running", False)

    # ----------------------------------------------------------------- loop
    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # keep the loop alive no matter what
                self._set("last_error", str(exc))
                log.exception("poll cycle failed")
            self._stop.wait(config.POLL_INTERVAL_SECONDS)

    # -------------------------------------------------- sudden-move watcher
    def _watcher_loop(self):
        """Scans enabled users' universes on its own faster cadence.

        A separate daemon thread (interval MOVERS_WATCH_INTERVAL_SECONDS) so
        big session moves alert within minutes instead of waiting for the
        hourly corporate-action poll. Skips the first cycle so it has a
        baseline, and only runs when at least one user enabled /watcher.
        """
        first = True
        while not self._stop.is_set():
            if not first:
                try:
                    self.run_watcher_once()
                except Exception as exc:  # never let the watcher die
                    self._set("last_error", config.redact(str(exc)))
                    log.warning("watcher cycle failed: %s", config.redact(exc))
            first = False
            self._stop.wait(config.MOVERS_WATCH_INTERVAL_SECONDS)

    def run_watcher_once(self) -> int:
        """One scan: alert any enabled user when a universe stock moves >= its
        threshold (session % from previous close). Returns alerts sent.

        Alerts are de-duplicated per chat per day (seen key `mwatch|...`), so
        a stock that keeps falling alerts once, not every cycle.
        """
        targets = watcher_mod.watcher_targets()
        if not targets:
            return 0
        today = today_ist()
        sent = 0
        uniq = watcher_mod.unique_watch_pairs(targets)
        if not uniq:
            return 0
        quotes = watcher_mod.fetch_quotes(uniq)
        for chat_id, sym, q, chg in watcher_mod.pending_alerts(targets, quotes, self._seen, today):
            try:
                send_message(
                    format_mover_alert(sym, q, chg),
                    chat_id=chat_id,
                    reply_markup=symbol_buttons([sym], "fund"),
                )
                self._seen.add(f"mwatch|{chat_id}|{today.isoformat()}|{sym.upper()}")
                sent += 1
            except NotifierError as exc:
                log.warning("watcher alert failed for %s: %s", sym, config.redact(exc))
        return sent

    def _collect_targets(self, only_chat: str | None) -> list[tuple[str, list]]:
        """Return [(chat_id, watchlist), ...] for every chat with a list."""
        targets = []
        app_watchlist = storage.load_watchlist()
        owner = str(config.TELEGRAM_CHAT_ID)
        if app_watchlist:
            targets.append((owner, app_watchlist))
        for chat_id, items in storage.load_subscriptions().items():
            if items and str(chat_id) != owner:
                targets.append((str(chat_id), items))
        if only_chat:
            targets = [(c, w) for c, w in targets if c == str(only_chat)]
        return targets

    def _filters_for(self, chat_id: str) -> list[str]:
        """The chat's action-type filters (valid types only)."""
        settings = storage.get_user_settings(chat_id)
        return [
            f.strip().lower()
            for f in settings.get("action_filters") or []
            if f.strip().lower() in ACTION_TYPES
        ]

    def _fetch_for_watchlist(self, unique_watchlist: list[dict]) -> tuple[list[dict], list[str], list[str]]:
        """Fetch per-symbol NSE + global BSE actions for the unique watchlist.

        Returns (all_actions, errors, warnings). Uses the same TTL-cached
        per-symbol failure logic so delisted/renamed symbols fail quietly.
        """
        global _nse_fetch_fail
        nse_symbols = [
            w["symbol"] for w in unique_watchlist
            if w.get("exchange", "").upper() == "NSE"
        ]
        bse_symbols = [
            w["symbol"] for w in unique_watchlist
            if w.get("exchange", "").upper() == "BSE"
        ]
        all_actions: list[dict] = []
        errors: list[str] = []
        warnings: list[str] = []

        if nse_symbols:
            def _fetch_nse_sym(sym):
                from ..sources import get_nse_corporate_actions

                try:
                    res = get_nse_corporate_actions(symbol=sym)
                    _nse_fetch_fail.pop(sym, None)
                    return res, None
                except Exception as exc:
                    now = time.monotonic()
                    last = _nse_fetch_fail.get(sym)
                    if last and now - last < _NSE_FETCH_FAIL_TTL:
                        # Known miss (delisted / non-equity symbol) - skip
                        # quietly instead of failing the whole cycle again.
                        return [], None
                    _nse_fetch_fail[sym] = now
                    return [], f"NSE:{sym}: {exc}"

            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(_fetch_nse_sym, sym): sym for sym in nse_symbols}
                for fut in as_completed(futures):
                    try:
                        res, err = fut.result()
                        if res:
                            all_actions.extend(res)
                        if err:
                            errors.append(err)
                    except Exception as exc:
                        errors.append(f"NSE thread error: {exc}")

        # Query BSE globally (when enabled)
        if bse_symbols and config.ENABLE_BSE:
            from ..sources import get_bse_corporate_actions
            from ..sources.errors import SourceError

            try:
                bse_actions = get_bse_corporate_actions()
                all_actions.extend(bse_actions)
            except SourceError as exc:
                warnings.append(f"BSE unavailable (blocked by their WAF): {exc}")
            except Exception as exc:
                errors.append(f"BSE: {exc}")
        return all_actions, errors, warnings

    def run_once(self, force: bool = False, only_chat: str | None = None) -> int:
        """Fetch, filter and notify. Returns number of messages sent.

        With force=True every matching action is sent again, even if it was
        already notified in the past (used by the /checknow command).
        With only_chat set, only that chat's own list is checked and alerted
        (so /checknow only re-sends to the person who asked).
        """
        targets = self._collect_targets(only_chat)
        owner = str(config.TELEGRAM_CHAT_ID)

        if not targets:
            log.info("poll cycle: no watchlists to check (only_chat=%s)", only_chat)
            self._set("last_message", "Watchlist is empty - nothing to check")
            self._set("last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._incr("cycle")
            return 0

        log.info(
            "poll cycle start: %d list(s) to check (only_chat=%s, force=%s)",
            len(targets), only_chat, force,
        )
        t0 = time.monotonic()

        # Collect unique watchlist stocks across all active targets
        unique_watchlist = []
        seen_keys = set()
        for chat_id, watchlist in targets:
            for item in watchlist:
                if not isinstance(item, dict):
                    continue
                key = (item.get("exchange", "").upper(), item.get("symbol", "").upper())
                if key not in seen_keys:
                    seen_keys.add(key)
                    unique_watchlist.append(item)

        all_actions, errors, warnings = self._fetch_for_watchlist(unique_watchlist)

        log.info(
            "poll cycle: fetched %d corporate action(s) for %d unique watchlist stock(s) in %.1fs (errors=%d, warnings=%d)",
            len(all_actions), len(unique_watchlist), time.monotonic() - t0, len(errors), len(warnings),
        )
        sent = 0
        today = today_ist()

        for chat_id, watchlist in targets:
            filters = self._filters_for(chat_id)
            log.info(
                "poll cycle: processing chat %s watchlist (%d stock(s), filters=%s)",
                chat_id, len(watchlist), ", ".join(filters) if filters else "all types",
            )

            # -------------------------------------------------- action alerts
            wanted = {
                (w.get("exchange", "").upper(), w.get("symbol", "").upper())
                for w in watchlist
                if isinstance(w, dict)
            }
            # Only actions in a relevant window may alert: upcoming ex-dates,
            # recently passed ones (payment/subscription still in progress), or
            # announced-but-undated. Ancient records (e.g. a 2018 dividend on a
            # 2026 watchlist) match symbol+type but are years stale - without
            # this window they would spam on every /checknow force re-send.
            matching = [
                a
                for a in all_actions
                if (a.get("exchange", "").upper(), a.get("symbol", "").upper())
                in wanted
                and (not filters or action_type(a.get("subject")) in filters)
                and (
                    within_reminder_window(a.get("ex_date"), today)
                    or recently_passed(a.get("ex_date"), today)
                    or parse_ex_date(a.get("ex_date")) is None
                )
            ]
            log.info(
                "poll cycle: chat %s has %d matching corporate action(s)",
                chat_id, len(matching),
            )
            if str(chat_id) == owner:
                self._set("last_results", matching)

            for action in matching:
                base = event_key(action)
                key = f"{chat_id}|{base}"
                already = key in self._seen or (str(chat_id) == owner and base in self._seen)
                action["new"] = not already
                if already and not force:
                    continue
                quote = get_quote(action["exchange"], action["symbol"])
                if quote:
                    action["quote"] = quote
                try:
                    send_message(
                        format_corporate_action(action), chat_id=chat_id
                    )
                    self._seen.add(key)
                    if str(chat_id) == owner:
                        self._seen.add(base)
                    sent += 1
                except NotifierError as exc:
                    errors.append(f"Telegram: {exc}")
                    break  # token misconfiguration - stop hammering the API

            # --------------------------------------------- ex-date reminders
            if config.REMINDER_DAYS > 0:
                for action in matching:
                    if not within_reminder_window(action.get("ex_date"), today):
                        continue
                    remind_key = f"remind|{chat_id}|{event_key(action)}"
                    if remind_key in self._seen and not force:
                        continue
                    quote = get_quote(action["exchange"], action["symbol"])
                    if quote:
                        action["quote"] = quote
                    try:
                        send_message(
                            format_reminder(action), chat_id=chat_id
                        )
                        self._seen.add(remind_key)
                        sent += 1
                    except NotifierError as exc:
                        errors.append(f"Telegram: {exc}")
                        break

            # -------------------------------------------------- price alerts
            try:
                threshold = float(storage.get_user_settings(chat_id).get("price_alert_pct") or 0.0)
            except (TypeError, ValueError):
                threshold = 0.0
            if threshold > 0:
                log.info(
                    "poll cycle: price alerts active for chat %s at +/-%.2f%%",
                    chat_id, threshold,
                )
                for item in watchlist:
                    if not isinstance(item, dict):
                        continue
                    day_key = (
                        f"price|{chat_id}|{item.get('exchange', '').upper()}"
                        f"|{item.get('symbol', '').upper()}|{today.isoformat()}"
                    )
                    if day_key in self._seen and not force:
                        continue
                    quote = get_quote(item.get("exchange", "NSE"), item.get("symbol", ""))
                    if not quote or quote.get("change_pct") is None:
                        continue
                    if abs(quote["change_pct"]) < threshold:
                        continue
                    try:
                        send_message(
                            format_price_alert(item, quote, threshold),
                            chat_id=chat_id,
                        )
                        self._seen.add(day_key)
                        sent += 1
                    except NotifierError as exc:
                        errors.append(f"Telegram: {exc}")
                        break

        if self._seen:
            try:
                storage.save_seen(self._seen)
            except Exception as exc:
                # Losing the dedupe cache means already-sent actions would be
                # re-sent next cycle - log it loudly so it isn't silent data loss.
                log.exception("save_seen failed: %s", exc)
                errors.append(f"seen cache: {exc}")

        total_sent = self.status["total_sent"] + sent
        self._set("total_sent", total_sent)
        self._set("last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._set("last_error", "; ".join(errors) if errors else None)
        self._set("warnings", warnings)
        active = ", ".join(active_fetchers().keys()) or "none"
        target_count = len(targets)
        self._set(
            "last_message",
            f"Checked {target_count} list(s) against [{active}], sent {sent} new.",
        )
        self._incr("cycle")
        log.info(
            "poll cycle finished: %d list(s) against [%s], sent %d new "
            "message(s), %d error(s)",
            target_count, active, sent, len(errors),
        )
        return sent

    # -------------------------------------------------------------- helpers
    def _set(self, key, value):
        with self._status_lock:
            self.status[key] = value

    def _incr(self, key):
        with self._status_lock:
            self.status[key] = self.status.get(key, 0) + 1


# module-level singleton used by the UI
poller = Poller()
