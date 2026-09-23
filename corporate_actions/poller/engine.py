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
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta

from .. import config, storage
from ..core.dates import today_ist
from ..market.hours import market_active, market_for_exchange, screen_available
from ..formatting import (
    format_corporate_action,
    format_mover_alert,
    format_price_alert,
    format_reminder,
)
from ..sources import get_best_quote, get_quote
from ..sources.types import ACTION_TYPES, action_type
from ..telegram.client import NotifierError, send_message
from ..telegram.markup import symbol_buttons
from . import watcher as watcher_module
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
_NSE_FETCH_FAIL_CACHE_SECONDS = 3600  # seconds - re-check the symbol hourly

# Session-day verdicts for the price-alert gate, cached per market per date:
# a mid-week exchange holiday (market hours say 'weekday, within session'
# but no trading happened) would otherwise re-fire Friday-style stale alerts
# on every cycle. One benchmark probe per market per day resolves it.
_session_day_cache: dict[tuple[str, str], bool] = {}


def _session_day(market: str, today) -> bool:
    """True when ``market`` really traded today (weekday+hours AND a session).

    Cached per (market, date) so the extra benchmark probe runs at most once
    per market per day, and only when the cheap clock check already passed.
    """
    key = (market, today.isoformat())
    if key not in _session_day_cache:
        from ..opening_report import data as ordata

        traded = ordata.has_session_on(market, today)
        # None (probe unavailable) counts as trading so a data-source outage
        # degrades to the old behaviour instead of silencing alerts all day.
        _session_day_cache[key] = True if traded is None else bool(traded)
    return _session_day_cache[key]


def _poll_quote(exchange: str, symbol: str) -> dict | None:
    """Live quote for the poll loop with free-API fallbacks.

    Yahoo first (previous-close change %, the daily-alert semantic), then
    NSE equity API, then Stooq — so a Yahoo outage never silently swallows
    a price alert or a corporate-action price line. Never raises.
    """
    try:
        quote = get_best_quote(exchange or "NSE", symbol or "")
        if quote and quote.get("price") is not None:
            return quote
    except Exception as error:
        log.info("poll quote fallback chain failed for %s:%s: %s", exchange, symbol, error)
    try:
        return get_quote(exchange or "NSE", symbol or "")
    except Exception:
        return None


class Poller:
    def __init__(self, push_state_callback=None):
        self._stop = threading.Event()
        self._thread = None
        self._watcher_thread = None
        # Called right after alerts are sent so the dedup keys reach GitHub
        # immediately. Without it the keys sit on the host's ephemeral disk
        # until the periodic flush - and a redeploy in between boots with the
        # stale seen-set and RE-SENDS every alert (the double-fire flood).
        self.push_state_callback = push_state_callback
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
        self._boot_monotonic = time.monotonic()
        # Flood-guard state: True while the committed seen-file looks stale.
        # The FIRST successful poll cycle clears it - by then every eligible
        # alert has been de-duplicated into self._seen (and pushed to GitHub
        # via push_state_callback), so redeploys can no longer re-fire.
        self._seen_stale = (self._newest_seen_age_days() or 0) > 2
        self._grace_notice_sent = False
        self._warn_if_stale_seen()

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

    # ------------------------------------------------- lifecycle
    def _loop(self):
        # Redeploy flood-guard: a fresh container whose committed seen-file is
        # stale would instantly re-fire every alert the user already received.
        # Hold the poll loop in a grace pause instead; the owner notice asks
        # for the GH_TOKEN fix. The FIRST cycle that does run de-duplicates
        # everything and clears the stale flag, ending the guard.
        if self._seen_stale and config.BOOT_FLOOD_GRACE_MINUTES > 0:
            grace = config.BOOT_FLOOD_GRACE_MINUTES * 60
            log.warning(
                "Stale dedup cache (%d day(s)) - holding alerts in grace for "
                "%d min after boot to avoid a redeploy re-fire flood. Fix "
                "GH_TOKEN so state pushes reach GitHub; the first poll cycle "
                "re-seeds dedup and clears the guard.",
                self._newest_seen_age_days(), config.BOOT_FLOOD_GRACE_MINUTES,
            )
            self._notify_owner_stale_grace()
            if self._stop.wait(grace):
                return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # keep the loop alive no matter what
                self._set("last_error", str(error))
                log.exception("poll cycle failed")
            self._stop.wait(self._poll_wait_seconds())

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
                except Exception as error:  # never let the watcher die
                    self._set("last_error", config.redact(str(error)))
                    log.warning("watcher cycle failed: %s", config.redact(error))
            first = False
            self._stop.wait(config.MOVERS_WATCH_INTERVAL_SECONDS)

    def run_watcher_once(self) -> int:
        """One scan: alert any enabled user when a universe stock moves >= its
        threshold (session % from previous close). Returns alerts sent.

        Alerts are de-duplicated per chat per day (seen key `mwatch|...`), so
        a stock that keeps falling alerts once, not every cycle.

        Market-hours gated: the watcher's universes are India-only, so the
        whole cycle is skipped outside the IST session (plus a 1-hour grace
        after the close). Otherwise a stale move from yesterday's session
        would re-alert after midnight when the daily dedup key resets.
        """
        if not market_active("in"):
            log.debug("watcher cycle skipped - India market closed")
            return 0
        # Flood-guard: the watcher fires on session moves; a stale dedup boot
        # would re-send them all. It stays quiet until the poller's first
        # post-grace cycle re-seeds dedup and lifts the guard.
        if self._seen_stale:
            log.info("watcher cycle skipped - flood-guard active (stale dedup boot)")
            return 0
        targets = watcher_module.watcher_targets()
        if not targets:
            return 0
        today = today_ist()
        sent = 0
        unique_pairs = watcher_module.unique_watch_pairs(targets)
        if not unique_pairs:
            return 0
        quotes = watcher_module.fetch_quotes(unique_pairs)
        for chat_id, symbol, quote, change in watcher_module.pending_alerts(targets, quotes, self._seen, today):
            if storage.is_quiet(chat_id):
                log.debug("watcher alert skipped for %s - chat is in quiet mode", chat_id)
                continue
            try:
                send_message(
                    format_mover_alert(symbol, quote, change),
                    chat_id=chat_id,
                    reply_markup=symbol_buttons([symbol], "fund"),
                )
                self._seen.add(f"mwatch|{chat_id}|{today.isoformat()}|{symbol.upper()}")
                sent += 1
            except NotifierError as error:
                log.warning("watcher alert failed for %s: %s", symbol, config.redact(error))
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
            targets = [(chat_id, watchlist) for chat_id, watchlist in targets if chat_id == str(only_chat)]
        return targets

    def _filters_for(self, chat_id: str) -> list[str]:
        """The chat's action-type filters (valid types only)."""
        settings = storage.get_user_settings(chat_id)
        return [
            filter.strip().lower()
            for filter in settings.get("action_filters") or []
            if filter.strip().lower() in ACTION_TYPES
        ]

    def _fetch_for_watchlist(self, unique_watchlist: list[dict]) -> tuple[list[dict], list[str], list[str]]:
        """Fetch per-symbol NSE + global BSE actions for the unique watchlist.

        Returns (all_actions, errors, warnings). Uses the same TTL-cached
        per-symbol failure logic so delisted/renamed symbols fail quietly.
        """
        nse_symbols = [
            watch_item["symbol"] for watch_item in unique_watchlist
            if watch_item.get("exchange", "").upper() == "NSE"
        ]
        bse_symbols = [
            watch_item["symbol"] for watch_item in unique_watchlist
            if watch_item.get("exchange", "").upper() == "BSE"
        ]
        all_actions: list[dict] = []
        errors: list[str] = []
        warnings: list[str] = []

        if nse_symbols:
            def _fetch_nse_symbol(symbol):
                from ..sources import get_nse_corporate_actions

                try:
                    result = get_nse_corporate_actions(symbol=symbol)
                    _nse_fetch_fail.pop(symbol, None)
                    return result, None
                except Exception as error:
                    now = time.monotonic()
                    last = _nse_fetch_fail.get(symbol)
                    if last and now - last < _NSE_FETCH_FAIL_CACHE_SECONDS:
                        # Known miss (delisted / non-equity symbol) - skip
                        # quietly instead of failing the whole cycle again.
                        return [], None
                    _nse_fetch_fail[symbol] = now
                    return [], f"NSE:{symbol}: {error}"

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(_fetch_nse_symbol, symbol): symbol for symbol in nse_symbols}
                for future in as_completed(futures):
                    try:
                        result, error = future.result()
                        if result:
                            all_actions.extend(result)
                        if error:
                            errors.append(error)
                    except Exception as error:
                        errors.append(f"NSE thread error: {error}")

        # Query BSE globally (when enabled)
        if bse_symbols and config.ENABLE_BSE:
            from ..sources import get_bse_corporate_actions
            from ..sources.errors import SourceError

            try:
                bse_actions = get_bse_corporate_actions()
                all_actions.extend(bse_actions)
            except SourceError as error:
                warnings.append(f"BSE unavailable (blocked by their WAF): {error}")
            except Exception as error:
                errors.append(f"BSE: {error}")
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

        # Flood-guard suppression: while the stale-boot guard is up the
        # poller still gathers data but sends NOTHING. Once the grace window
        # has passed, this same cycle re-seeds the dedup cache (at the tail
        # of run_once) - which is what permanently ends the re-fire flood.
        # An explicit /checknow (force) always bypasses the guard: the user
        # asked for it by name.
        suppress = (not force) and self._seen_stale
        seeding = suppress and self._grace_elapsed()
        if suppress and not seeding:
            log.info(
                "flood-guard: inside the %d min post-boot grace - "
                "scanning but not alerting",
                config.BOOT_FLOOD_GRACE_MINUTES,
            )
        elif seeding:
            log.info("flood-guard: grace elapsed - re-seeding dedup without re-sending")

        log.info(
            "poll cycle start: %d list(s) to check (only_chat=%s, force=%s)",
            len(targets), only_chat, force,
        )
        started_at = time.monotonic()

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
            len(all_actions), len(unique_watchlist), time.monotonic() - started_at, len(errors), len(warnings),
        )
        sent = 0
        today = today_ist()

        for chat_id, watchlist in targets:
            filters = self._filters_for(chat_id)
            # Per-chat push gates: /corpactions off (or /alertfilters off)
            # silences corporate-action pushes; /quiet pauses EVERYTHING
            # temporarily. On-demand queries are unaffected - this only gates
            # the automatic sends.
            ca_on = storage.ca_alerts_enabled(chat_id)
            quiet = storage.is_quiet(chat_id)
            log.info(
                "poll cycle: processing chat %s watchlist (%d stock(s), filters=%s, ca=%s, quiet=%s)",
                chat_id, len(watchlist), ", ".join(filters) if filters else "all types",
                ca_on, quiet,
            )

            # -------------------------------------------------- action alerts
            wanted = {
                (watch_item.get("exchange", "").upper(), watch_item.get("symbol", "").upper())
                for watch_item in watchlist
                if isinstance(watch_item, dict)
            }
            # Only actions in a relevant window may alert: upcoming ex-dates,
            # recently passed ones (payment/subscription still in progress), or
            # announced-but-undated. Ancient records (e.g. a 2018 dividend on a
            # 2026 watchlist) match symbol+type but are years stale - without
            # this window they would spam on every /checknow force re-send.
            matching = [
                action
                for action in all_actions
                if (action.get("exchange", "").upper(), action.get("symbol", "").upper())
                in wanted
                and (not filters or action_type(action.get("subject")) in filters)
                and (
                    within_reminder_window(action.get("ex_date"), today)
                    or recently_passed(action.get("ex_date"), today)
                    or parse_ex_date(action.get("ex_date")) is None
                )
            ]
            log.info(
                "poll cycle: chat %s has %d matching corporate action(s)",
                chat_id, len(matching),
            )
            if str(chat_id) == owner:
                self._set("last_results", matching)

            if ca_on and not quiet and not suppress:
                for action in matching:
                    base = event_key(action)
                    key = f"{chat_id}|{base}"
                    already = key in self._seen or (str(chat_id) == owner and base in self._seen)
                    action["new"] = not already
                    if already and not force:
                        continue
                    quote = _poll_quote(action["exchange"], action["symbol"])
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
                    except NotifierError as error:
                        errors.append(f"Telegram: {error}")
                        break  # token misconfiguration - stop hammering the API

            # --------------------------------------------- ex-date reminders
            if config.REMINDER_DAYS > 0 and ca_on and not quiet and not suppress:
                for action in matching:
                    if not within_reminder_window(action.get("ex_date"), today):
                        continue
                    remind_key = f"remind|{chat_id}|{event_key(action)}"
                    if remind_key in self._seen and not force:
                        continue
                    quote = _poll_quote(action["exchange"], action["symbol"])
                    if quote:
                        action["quote"] = quote
                    try:
                        send_message(
                            format_reminder(action), chat_id=chat_id
                        )
                        self._seen.add(remind_key)
                        sent += 1
                    except NotifierError as error:
                        errors.append(f"Telegram: {error}")
                        break

            # -------------------------------------------------- price alerts
            try:
                threshold = float(storage.get_user_settings(chat_id).get("price_alert_pct") or 0.0)
            except (TypeError, ValueError):
                threshold = 0.0
            if threshold > 0 and not quiet and not suppress:
                log.info(
                    "poll cycle: price alerts active for chat %s at +/-%.2f%%",
                    chat_id, threshold,
                )
                for item in watchlist:
                    if not isinstance(item, dict):
                        continue
                    item_market = market_for_exchange(item.get("exchange"))
                    # Session-day gate: the quote's change % is THIS session
                    # vs the previous close. Outside that session (weekend,
                    # holiday, or before the next open) Yahoo still serves
                    # the last completed session's move, which would fire
                    # under TODAY's dedup key - Friday's +3% re-alerting all
                    # Saturday, and again Monday pre-open. Skip items whose
                    # market did not trade today; seen keys stay date-stamped
                    # so the alert still fires once on the next real session.
                    if not screen_available(item_market):
                        continue
                    if not _session_day(item_market, today):
                        continue
                    day_key = (
                        f"price|{chat_id}|{item.get('exchange', '').upper()}"
                        f"|{item.get('symbol', '').upper()}|{today.isoformat()}"
                    )
                    if day_key in self._seen and not force:
                        continue
                    quote = _poll_quote(item.get("exchange", "NSE"), item.get("symbol", ""))
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
                    except NotifierError as error:
                        errors.append(f"Telegram: {error}")
                        break

        if self._seen:
            try:
                storage.save_seen(self._seen)
            except Exception as error:
                # Losing the dedupe cache means already-sent actions would be
                # re-sent next cycle - log it loudly so it isn't silent data loss.
                log.exception("save_seen failed: %s", error)
                errors.append(f"seen cache: {error}")
        if seeding:
            # First post-grace cycle: mark everything currently eligible as
            # already-notified WITHOUT sending. The user already received
            # these alerts before the redeploy; re-seeding is what makes
            # future redeploys quiet even while GitHub pushes are broken.
            seeded = 0
            # Reuse the same eligibility rules as the alert loops above.
            for chat_id, watchlist in targets:
                filters = self._filters_for(chat_id)
                wanted = {
                    (wi.get("exchange", "").upper(), wi.get("symbol", "").upper())
                    for wi in watchlist if isinstance(wi, dict)
                }
                for action in all_actions:
                    if (action.get("exchange", "").upper(), action.get("symbol", "").upper()) not in wanted:
                        continue
                    if filters and action_type(action.get("subject")) not in filters:
                        continue
                    if not (
                        within_reminder_window(action.get("ex_date"), today)
                        or recently_passed(action.get("ex_date"), today)
                        or parse_ex_date(action.get("ex_date")) is None
                    ):
                        continue
                    self._seen.add(f"{chat_id}|{event_key(action)}")
                    if str(chat_id) == owner:
                        self._seen.add(event_key(action))
                    seeded += 1
            log.info("flood-guard re-seed: %d eligible action(s) marked as notified", seeded)
            self._seen_stale = False
        if sent:
            # Alerts went out - persist the dedup keys to GitHub NOW rather
            # than waiting for the periodic flush. A redeploy that lands
            # before the flush boots with the old seen-set and re-sends
            # every alert the user already received.
            self._persist_seen_to_github()

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
    def _grace_elapsed(self) -> bool:
        """True once the post-boot flood-guard window has passed."""
        grace = config.BOOT_FLOOD_GRACE_MINUTES * 60
        if grace <= 0:
            return True
        return (time.monotonic() - self._boot_monotonic) >= grace

    def _poll_wait_seconds(self) -> int:
        """Poll interval plus bounded random jitter (thundering-herd guard)."""
        jitter = config.POLL_JITTER_SECONDS
        if jitter > 0:
            return config.POLL_INTERVAL_SECONDS + random.randint(0, jitter)
        return config.POLL_INTERVAL_SECONDS

    def _notify_owner_stale_grace(self) -> None:
        """One-time owner notice that alerts are paused by the flood-guard."""
        if self._grace_notice_sent or not config.TELEGRAM_CHAT_ID:
            return
        self._grace_notice_sent = True
        try:
            send_message(
                "<b>\u26a0\ufe0f Alert flood-guard active</b>\n"
                "The server restarted with an out-of-date alert memory "
                "(dedup state on GitHub is stale - state pushes have been "
                "failing, usually an expired GH_TOKEN).\n\n"
                "Automatic alerts are <b>paused "
                f"{config.BOOT_FLOOD_GRACE_MINUTES} min</b> so this restart "
                "does not re-send everything you already got.\n"
                "Fix <code>GH_TOKEN</code> on the host (fine-grained PAT, "
                "Contents: read/write), then send /checknow to get any "
                "genuinely new alerts now.",
                chat_id=config.TELEGRAM_CHAT_ID,
            )
        except NotifierError as error:
            log.warning("flood-guard notice failed: %s", config.redact(error))

    def _persist_seen_to_github(self):
        """Best-effort immediate push of the dedup state after alerts.

        Never raises: a push failure must not break the alert cycle that
        just succeeded. The periodic flush retries anyway.
        """
        if not self.push_state_callback:
            return
        try:
            self.push_state_callback()
        except Exception as error:
            log.warning("post-alert state push failed: %s", config.redact(error))

    def _warn_if_stale_seen(self):
        """Boot-time warning when the dedup cache looks frozen.

        Every key carries a date (ISO or dd-Mmm-yyyy); if the newest is more
        than two days old the host's pushes have not been landing - the exact
        condition that makes every redeploy re-fire old alerts. Warning at
        boot makes the failure visible in the deploy log instead of only in
        the user's Telegram.
        """
        try:
            age_days = self._newest_seen_age_days()
            if age_days is None or age_days <= 2:
                return
            log.warning(
                "seen_actions.json is %d day(s) stale (newest key %s) - "
                "recent alert dedup keys never reached GitHub, so alerts "
                "WILL re-fire on redeploy. Check GH_TOKEN / push_state logs.",
                age_days, date.today() - timedelta(days=age_days),
            )
        except Exception:  # diagnostics must never break boot
            log.debug("seen staleness check failed", exc_info=True)

    def _newest_seen_age_days(self):
        """Age in days of the newest dated dedup key; None when undated.

        Keys carry either an ISO date or a dd-Mmm-yyyy date (event keys); the
        newest one approximates when dedup state last reached GitHub.
        """
        import datetime as _dt
        import re

        newest = None
        for key in self._seen:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(key))
            if match:
                day = _dt.date.fromisoformat(match.group(1))
            else:
                match = re.search(r"(\d{2})-([A-Za-z]{3})-(\d{4})", str(key))
                if not match:
                    continue
                day = _dt.datetime.strptime(
                    f"{match.group(1)}-{match.group(2)}-{match.group(3)}", "%d-%b-%Y"
                ).date()
            if newest is None or day > newest:
                newest = day
        if newest is None:
            return None
        return (_dt.date.today() - newest).days

    def _set(self, key, value):
        with self._status_lock:
            self.status[key] = value

    def _incr(self, key):
        with self._status_lock:
            self.status[key] = self.status.get(key, 0) + 1


# module-level singleton used by the UI
poller = Poller()
