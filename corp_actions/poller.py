"""Background poller: fetches corporate actions and pushes new ones to Telegram.

The poller runs in a daemon thread, reads the persisted watchlist every cycle,
and keeps a status dict that the Streamlit UI can display.

Beyond new-action alerts it also supports:
  * ex-date reminders (warn N days before the ex-date, once per action)
  * price-move alerts (notify when a watched stock moves beyond a threshold)
  * per-user action-type filters (dividend/bonus/split/rights/buyback only)
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from . import config, notifier, sources, storage

log = logging.getLogger(__name__)

FETCHERS = {
    "NSE": sources.get_nse_corporate_actions,
    "BSE": sources.get_bse_corporate_actions,
}


def _active_fetchers() -> dict:
    """Enabled sources (BSE optional via ENABLE_BSE)."""
    fetchers = dict(FETCHERS)
    if not config.ENABLE_BSE:
        fetchers.pop("BSE", None)
    return fetchers


def event_key(action: dict) -> str:
    return "|".join(
        [
            action.get("exchange", ""),
            action.get("symbol", ""),
            action.get("subject", ""),
            action.get("ex_date", ""),
            action.get("record_date", ""),
        ]
    )


def fetch_all_actions() -> tuple[list[dict], list[str], list[str]]:
    """Fetch corporate actions from all enabled sources.

    Returns (actions, errors, warnings). Source failures degrade gracefully:
    BSE is a warning, anything else is an error.
    """
    errors, warnings, all_actions = [], [], []
    for exchange, fetcher in _active_fetchers().items():
        try:
            actions = fetcher()
            for a in actions:
                a["exchange"] = exchange
            all_actions.extend(actions)
        except sources.SourceError as exc:
            if exchange == "BSE":
                warnings.append(f"BSE unavailable (blocked by their WAF): {exc}")
            else:
                errors.append(f"{exchange}: {exc}")
        except Exception as exc:  # keep the whole cycle alive on unexpected bugs
            log.exception("fetcher %s raised unexpectedly", exchange)
            errors.append(f"{exchange}: {exc}")
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
        w["symbol"] for w in watchlist
        if w.get("exchange", "").upper() == "NSE"
    ]
    bse_symbols = [
        w["symbol"] for w in watchlist
        if w.get("exchange", "").upper() == "BSE"
    ]

    all_actions: list[dict] = []

    # Query NSE per-symbol (parallel) to get full history for each watchlist stock
    if nse_symbols:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_nse(sym):
            try:
                return sources.get_nse_corporate_actions(symbol=sym)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_fetch_nse, sym): sym for sym in nse_symbols}
            for fut in as_completed(futures):
                try:
                    all_actions.extend(fut.result())
                except Exception:
                    pass

    # Query BSE globally (when enabled)
    if bse_symbols and config.ENABLE_BSE:
        try:
            bse_actions = sources.get_bse_corporate_actions()
            all_actions.extend(bse_actions)
        except Exception:
            pass

    # Filter to only watchlist symbols
    wanted = {
        (w.get("exchange", "").upper(), w.get("symbol", "").upper())
        for w in watchlist
    }
    return [
        a
        for a in all_actions
        if (a.get("exchange", "").upper(), a.get("symbol", "").upper()) in wanted
    ]


def parse_ex_date(value) -> date | None:
    """Parse an ISO ex-date, returning None when unset/invalid."""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


RECENT_PASSED_DAYS = 30  # how far back /next reports recently passed ex-dates


def within_reminder_window(
    ex_date, today: date | None = None, days: int | None = None
) -> bool:
    """True when ex_date is today or within the reminder window ahead."""
    parsed = parse_ex_date(ex_date)
    if parsed is None:
        return False
    today = today or config.today_ist()
    days = config.REMINDER_DAYS if days is None else days
    if days <= 0:
        return False
    return today <= parsed <= today + timedelta(days=days)


def recently_passed(
    ex_date, today: date | None = None, days: int | None = None
) -> bool:
    """True when ex_date fell within the recent lookback window (ex-date
    passed in the last `days` days, today excluded).

    Used by /next to surface in-progress actions - a rights issue whose
    ex-date has just passed (subscription still open) or a dividend whose
    payment is still pending - that a pure upcoming-ex-date view misses.
    """
    parsed = parse_ex_date(ex_date)
    if parsed is None:
        return False
    today = today or config.today_ist()
    days = RECENT_PASSED_DAYS if days is None else days
    if days <= 0:
        return False
    return today - timedelta(days=days) <= parsed < today


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

    def _watcher_targets(self) -> list[tuple[str, dict]]:
        """[(chat_id, watcher_settings)] for every chat with the watcher on."""
        out = []
        for chat_id, settings in storage.load_settings().items():
            w = settings.get("watcher") or {}
            if w.get("enabled") and float(w.get("threshold") or 0) > 0:
                out.append((str(chat_id), w))
        return out

    def _watcher_symbols(self, chat_id: str, universe: str) -> list[str]:
        """Resolve a watcher universe to symbols: nifty100 / nifty500 / mylist."""
        u = (universe or "nifty100").lower()
        if u in ("nifty500", "500", "all"):
            return sources.get_index_universe("nifty500") or []
        if u in ("mylist", "watchlist"):
            items = storage.get_user_list(chat_id)
            return [i["symbol"] for i in items if isinstance(i, dict)]
        return sources.get_index_universe("nifty100") or []

    def run_watcher_once(self) -> int:
        """One scan: alert any enabled user when a universe stock moves >= its
        threshold (session % from previous close). Returns alerts sent.

        Alerts are de-duplicated per chat per day (seen key `mwatch|...`), so
        a stock that keeps falling alerts once, not every cycle.
        """
        targets = self._watcher_targets()
        if not targets:
            return 0
        today = config.today_ist()
        sent = 0

        # Unique symbols across all enabled users (quote cache dedupes fetches)
        uniq: list[tuple[str, str]] = []
        seen_syms = set()
        for chat_id, w in targets:
            for sym in self._watcher_symbols(chat_id, w.get("universe", "nifty100")):
                key = (chat_id, sym.upper())
                if key not in seen_syms:
                    seen_syms.add(key)
                    uniq.append(key)
        if not uniq:
            return 0

        quotes: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {
                ex.submit(sources.get_quote, "NSE", sym): (chat_id, sym)
                for chat_id, sym in uniq
            }
            for fut in as_completed(futs):
                try:
                    q = fut.result()
                except Exception:
                    q = None
                if q and q.get("change_pct") is not None:
                    quotes[futs[fut][1].upper()] = q

        for chat_id, w in targets:
            threshold = float(w.get("threshold") or 0)
            for sym in self._watcher_symbols(chat_id, w.get("universe", "nifty100")):
                q = quotes.get(sym.upper())
                if not q or q.get("change_pct") is None:
                    continue
                chg = float(q["change_pct"])
                if abs(chg) < threshold:
                    continue
                key = f"mwatch|{chat_id}|{today.isoformat()}|{sym.upper()}"
                if key in self._seen:
                    continue
                try:
                    notifier.send_message(
                        notifier.format_mover_alert(sym, q, chg),
                        chat_id=chat_id,
                        reply_markup=notifier.symbol_buttons([sym], "fund"),
                    )
                    self._seen.add(key)
                    sent += 1
                except notifier.NotifierError as exc:
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

        # Query NSE per-symbol (parallel) to get full history for each watchlist stock
        if nse_symbols:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_nse(sym):
                try:
                    return sources.get_nse_corporate_actions(symbol=sym), None
                except Exception as exc:
                    return [], f"NSE:{sym}: {exc}"

            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(_fetch_nse, sym): sym for sym in nse_symbols}
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
            try:
                bse_actions = sources.get_bse_corporate_actions()
                all_actions.extend(bse_actions)
            except sources.SourceError as exc:
                warnings.append(f"BSE unavailable (blocked by their WAF): {exc}")
            except Exception as exc:
                errors.append(f"BSE: {exc}")

        log.info(
            "poll cycle: fetched %d corporate action(s) for %d unique watchlist stock(s) in %.1fs (errors=%d, warnings=%d)",
            len(all_actions), len(unique_watchlist), time.monotonic() - t0, len(errors), len(warnings),
        )
        sent = 0
        today = config.today_ist()

        for chat_id, watchlist in targets:
            settings = storage.get_user_settings(chat_id)
            filters = [
                f.strip().lower()
                for f in settings.get("action_filters") or []
                if f.strip().lower() in sources.ACTION_TYPES
            ]
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
                and (not filters or sources.action_type(a.get("subject")) in filters)
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
                quote = sources.get_quote(action["exchange"], action["symbol"])
                if quote:
                    action["quote"] = quote
                try:
                    notifier.send_message(
                        notifier.format_corporate_action(action), chat_id=chat_id
                    )
                    self._seen.add(key)
                    if str(chat_id) == owner:
                        self._seen.add(base)
                    sent += 1
                except notifier.NotifierError as exc:
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
                    quote = sources.get_quote(action["exchange"], action["symbol"])
                    if quote:
                        action["quote"] = quote
                    try:
                        notifier.send_message(
                            notifier.format_reminder(action), chat_id=chat_id
                        )
                        self._seen.add(remind_key)
                        sent += 1
                    except notifier.NotifierError as exc:
                        errors.append(f"Telegram: {exc}")
                        break

            # -------------------------------------------------- price alerts
            try:
                threshold = float(settings.get("price_alert_pct") or 0.0)
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
                    quote = sources.get_quote(item.get("exchange", "NSE"), item.get("symbol", ""))
                    if not quote or quote.get("change_pct") is None:
                        continue
                    if abs(quote["change_pct"]) < threshold:
                        continue
                    try:
                        notifier.send_message(
                            notifier.format_price_alert(item, quote, threshold),
                            chat_id=chat_id,
                        )
                        self._seen.add(day_key)
                        sent += 1
                    except notifier.NotifierError as exc:
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
        active = ", ".join(_active_fetchers().keys()) or "none"
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
