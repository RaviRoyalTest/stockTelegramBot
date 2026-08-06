"""Background poller: fetches corporate actions and pushes new ones to Telegram.

The poller runs in a daemon thread, reads the persisted watchlist every cycle,
and keeps a status dict that the Streamlit UI can display.
"""
import logging
import threading
import time
from datetime import datetime

from . import config, notifier, sources, storage

log = logging.getLogger(__name__)

FETCHERS = {"NSE": sources.get_nse_corporate_actions}


def _active_fetchers() -> dict:
    fetchers = {"NSE": sources.get_nse_corporate_actions}
    if config.ENABLE_BSE:
        fetchers["BSE"] = sources.get_bse_corporate_actions
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


class Poller:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
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
        self._set("running", True)
        self._set("last_message", "Poller started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
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

    def run_once(self, force: bool = False) -> int:
        """Fetch, filter and notify. Returns number of messages sent.

        With force=True every matching action is sent again, even if it was
        already notified in the past (used by the /checknow command).
        """
        targets = []  # (chat_id, watchlist)
        app_watchlist = storage.load_watchlist()
        owner = str(config.TELEGRAM_CHAT_ID)
        if app_watchlist:
            targets.append((owner, app_watchlist))
        for chat_id, items in storage.load_subscriptions().items():
            if items and str(chat_id) != owner:
                targets.append((str(chat_id), items))

        if not targets:
            self._set("last_message", "Watchlist is empty - nothing to check")
            self._set("last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._incr("cycle")
            return 0

        errors = []
        warnings = []
        all_actions = []
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

        sent = 0
        owner_results = []
        for chat_id, watchlist in targets:
            wanted = {(w["exchange"].upper(), w["symbol"].upper()) for w in watchlist}
            matching = [
                a for a in all_actions
                if (a.get("exchange", "").upper(), a.get("symbol", "").upper()) in wanted
            ]
            if chat_id == owner:
                owner_results = matching

            for action in matching:
                base = event_key(action)
                key = f"{chat_id}|{base}"
                already = key in self._seen or (chat_id == owner and base in self._seen)
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
                    if chat_id == owner:
                        self._seen.add(base)
                    sent += 1
                except notifier.NotifierError as exc:
                    errors.append(f"Telegram: {exc}")
                    break  # token misconfiguration - stop hammering the API

        if self._seen:
            storage.save_seen(self._seen)

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
        self._set("last_results", owner_results)
        self._incr("cycle")
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
