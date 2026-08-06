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

    def run_once(self) -> int:
        """Fetch, filter and notify. Returns number of messages sent."""
        watchlist = storage.load_watchlist()
        if not watchlist:
            self._set("last_message", "Watchlist is empty - nothing to check")
            self._set("last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._incr("cycle")
            return 0

        wanted = {(w["exchange"].upper(), w["symbol"].upper()) for w in watchlist}

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

        matching = [
            a for a in all_actions
            if (a.get("exchange", "").upper(), a.get("symbol", "").upper()) in wanted
        ]

        for action in matching:
            quote = sources.get_quote(action["exchange"], action["symbol"])
            if quote:
                action["quote"] = quote

        sent = 0
        for action in matching:
            key = event_key(action)
            action["new"] = key not in self._seen
            if key in self._seen:
                continue
            try:
                notifier.send_message(notifier.format_corporate_action(action))
                self._seen.add(key)
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
        self._set(
            "last_message",
            f"Checked {len(watchlist)} watchlist item(s) against [{active}], "
            f"found {len(matching)} matching action(s), sent {sent} new.",
        )
        self._set("last_results", matching)
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
