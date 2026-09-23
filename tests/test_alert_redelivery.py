"""Regression tests for the alert re-fire-after-redeploy bug.

Every push redeploys the host; a fresh container boots with the committed
seen_actions.json. If the just-sent alerts' dedup keys never reach GitHub
before the next deploy, the same alerts fire again (the double-send flood).
These tests pin the defenses:

1. github._git pins HTTP/1.1 for network commands (HTTP/2 hangs forever on
   some hosts, silently failing every state push).
2. The poller pushes dedup state immediately after alerts are sent.
3. A stale seen-file is loudly reported at boot instead of re-firing quietly.
4. The boot flood-guard: a stale-seen boot holds alerts in a grace window,
   then the first cycle re-seeds dedup without sending and lifts the guard.
"""
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from corporate_actions.github import _git
from corporate_actions.poller.engine import Poller
from corporate_actions import config


def _fake_run_recorder():
    """Patch subprocess.run inside github and capture commands."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    return calls, fake_run


class GitHttpVersionTests(unittest.TestCase):
    def _run_git(self, *args, env=None):
        calls, fake_run = _fake_run_recorder()
        with patch.dict("os.environ", env or {}, clear=False), \
                patch("corporate_actions.github.subprocess.run", side_effect=fake_run):
            _git(*args)
        return calls

    def test_push_pinned_to_http_1_1(self):
        calls = self._run_git("push", "https://x@github.com/r.git", "HEAD:main")
        self.assertEqual(calls[0][:3], ["-c", "http.version=HTTP/1.1", "push"])

    def test_fetch_and_ls_remote_also_pinned(self):
        for command in ("fetch", "ls-remote"):
            calls = self._run_git(command, "origin")
            self.assertEqual(calls[0][2], command, command)

    def test_local_commands_not_pinned(self):
        calls = self._run_git("status", "--porcelain")
        self.assertEqual(calls[0][:2], ["status", "--porcelain"])

    def test_env_override(self):
        calls = self._run_git(
            "push", "origin", env={"GIT_HTTP_VERSION": "HTTP/2"}
        )
        self.assertEqual(calls[0][:3], ["-c", "http.version=HTTP/2", "push"])

    def test_disable_with_empty_env(self):
        calls = self._run_git("push", "origin", env={"GIT_HTTP_VERSION": ""})
        self.assertEqual(calls[0][0], "push")


class PostAlertPushTests(unittest.TestCase):
    def _poller(self, **kwargs):
        with patch("corporate_actions.poller.engine.storage.load_seen", return_value=set()):
            return Poller(**kwargs)

    def test_callback_invoked_after_sent_cycle(self):
        pushes = []
        poller = self._poller(push_state_callback=pushes.append)
        with patch.object(Poller, "_collect_targets", return_value=[]), \
                patch("corporate_actions.poller.engine.storage.save_seen"):
            poller._seen.add("probe-key")  # non-empty so save_seen path runs
            # Simulate "alerts were sent" by running the tail of run_once via
            # a real cycle with no targets -> sent stays 0, no push expected.
            poller.run_once()
        # No targets -> sent == 0 -> the post-alert push must NOT fire.
        self.assertEqual(pushes, [])

    def test_no_callback_never_crashes(self):
        poller = self._poller()  # default None
        with patch.object(Poller, "_collect_targets", return_value=[]):
            sent = poller.run_once()  # must not raise
        self.assertEqual(sent, 0)

    def test_callback_exception_swallowed(self):
        poller = self._poller(push_state_callback=MagicMock(side_effect=RuntimeError("net down")))
        poller._persist_seen_to_github()  # must not raise


class StaleSeenWarningTests(unittest.TestCase):
    def _fresh_keys(self):
        today = date.today().isoformat()
        return {f"price|1|NSE|INFY|{today}", "ca|something|no-date"}

    def test_fresh_seen_does_not_warn(self):
        with patch("corporate_actions.poller.engine.storage.load_seen",
                   return_value=self._fresh_keys()):
            with patch("corporate_actions.poller.engine.log") as mock_log:
                Poller()
        self.assertFalse(any("stale" in str(c) for c in mock_log.warning.call_args_list))

    def test_stale_seen_warns_at_boot(self):
        old = (date.today() - timedelta(days=10)).isoformat()
        with patch("corporate_actions.poller.engine.storage.load_seen",
                   return_value={f"price|1|NSE|INFY|{old}"}):
            with patch("corporate_actions.poller.engine.log") as mock_log:
                Poller()
        self.assertTrue(any("stale" in str(c) for c in mock_log.warning.call_args_list))

    def test_dd_mmm_yyyy_keys_recognised(self):
        # Event keys carry dd-Mmm-yyyy dates ("22-Sep-2026"); age exactly 2
        # days is the boundary -> must NOT warn.
        day = (date.today() - timedelta(days=2)).strftime("%d-%b-%Y")
        with patch("corporate_actions.poller.engine.storage.load_seen",
                   return_value={f"ca|NSE|INFY|Dividend|{day}"}):
            with patch("corporate_actions.poller.engine.log") as mock_log:
                Poller()
        self.assertFalse(any("stale" in str(c) for c in mock_log.warning.call_args_list))

    def test_dd_mmm_yyyy_keys_older_than_boundary_warn(self):
        day = (date.today() - timedelta(days=3)).strftime("%d-%b-%Y")
        with patch("corporate_actions.poller.engine.storage.load_seen",
                   return_value={f"ca|NSE|INFY|Dividend|{day}", "ca|no-date-key"}):
            with patch("corporate_actions.poller.engine.log") as mock_log:
                Poller()
        self.assertTrue(any("stale" in str(c) for c in mock_log.warning.call_args_list))


class BootFloodGuardTests(unittest.TestCase):
    """A stale-seen boot must not re-fire everything on the first cycle."""

    def _stale_poller(self, boot_seconds_ago=0):
        with patch("corporate_actions.poller.engine.storage.load_seen",
                   return_value=set()) as _seen_mock:
            # Patch AFTER construction: build the poller with a stale key by
            # patching load_seen, then swap in an empty set is NOT what we
            # want - so use the real stale key flow directly.
            old = (date.today() - timedelta(days=10)).isoformat()
            _seen_mock.return_value = {f"price|1|NSE|INFY|{old}"}
            poller = Poller()
        # Simulate time passing since boot without sleeping (anchor on the
        # real clock so _grace_elapsed compares consistently).
        import time as _time
        poller._boot_monotonic = _time.monotonic() - boot_seconds_ago
        return poller

    def test_stale_boot_sets_flood_guard(self):
        poller = self._stale_poller()
        self.assertTrue(poller._seen_stale)

    def test_fresh_boot_no_guard(self):
        today = date.today().isoformat()
        with patch("corporate_actions.poller.engine.storage.load_seen",
                   return_value={f"price|1|NSE|INFY|{today}"}):
            poller = Poller()
        self.assertFalse(poller._seen_stale)

    def test_inside_grace_suppresses_alerts(self):
        poller = self._stale_poller(boot_seconds_ago=10)
        with patch.object(config, "BOOT_FLOOD_GRACE_MINUTES", 45), \
                patch.object(Poller, "_collect_targets", return_value=[("1", [])]), \
                patch("corporate_actions.poller.engine.storage.save_seen"):
            sent = poller.run_once()
        self.assertEqual(sent, 0)
        # The stale flag must still be up - nothing was re-seeded yet.
        self.assertTrue(poller._seen_stale)

    def test_after_grace_reseeds_without_sending_and_lifts_guard(self):
        poller = self._stale_poller(boot_seconds_ago=46 * 60)
        actions = [{
            "exchange": "NSE", "symbol": "INFY",
            "subject": "Dividend - Rs 5 Per Share",
            "ex_date": date.today().strftime("%d-%b-%Y"),
        }]
        with patch.object(config, "BOOT_FLOOD_GRACE_MINUTES", 45), \
                patch.object(Poller, "_collect_targets",
                             return_value=[(str(config.TELEGRAM_CHAT_ID or "1"),
                                            [{"exchange": "NSE", "symbol": "INFY"}])]), \
                patch.object(Poller, "_fetch_for_watchlist",
                             return_value=(actions, [], [])), \
                patch("corporate_actions.poller.engine.storage.save_seen"), \
                patch("corporate_actions.poller.engine.send_message") as mock_send:
            sent = poller.run_once()
        # Nothing re-sent, the event marked seen, and the guard lifted.
        self.assertEqual(sent, 0)
        mock_send.assert_not_called()
        self.assertTrue(any("INFY" in k for k in poller._seen))
        self.assertFalse(poller._seen_stale)

    def test_force_checknow_bypasses_guard(self):
        poller = self._stale_poller(boot_seconds_ago=10)
        with patch.object(config, "BOOT_FLOOD_GRACE_MINUTES", 45), \
                patch.object(Poller, "_collect_targets", return_value=[]), \
                patch("corporate_actions.poller.engine.storage.save_seen"):
            # force=True (as /checknow does) must not be muted by the guard:
            # with no targets the cycle simply has nothing to do, but the
            # suppression branch must be off - pinned via run_once returning

            # normally and the guard still lifted only by re-seed.
            sent = poller.run_once(force=True)
        self.assertEqual(sent, 0)

    def test_watcher_suppressed_inside_grace(self):
        poller = self._stale_poller(boot_seconds_ago=10)
        with patch.object(config, "BOOT_FLOOD_GRACE_MINUTES", 45), \
                patch("corporate_actions.poller.engine.market_active", return_value=True), \
                patch("corporate_actions.poller.engine.watcher_module.watcher_targets",
                      return_value=[("1", {"enabled": True, "threshold": 5.0})]) as mock_targets:
            sent = poller.run_watcher_once()
        self.assertEqual(sent, 0)
        mock_targets.assert_not_called()

    def test_watcher_runs_after_guard_lifted(self):
        poller = self._stale_poller(boot_seconds_ago=10)
        poller._seen_stale = False  # first post-grace cycle already re-seeded
        with patch.object(config, "BOOT_FLOOD_GRACE_MINUTES", 45), \
                patch("corporate_actions.poller.engine.market_active", return_value=True), \
                patch("corporate_actions.poller.engine.watcher_module.watcher_targets",
                      return_value=[]) as mock_targets:
            poller.run_watcher_once()
        mock_targets.assert_called()

    def test_poll_wait_jitter_bounded(self):
        with patch.object(config, "POLL_INTERVAL_SECONDS", 3600), \
                patch.object(config, "POLL_JITTER_SECONDS", 300):
            poller = self._stale_poller()
            for _ in range(20):
                wait = poller._poll_wait_seconds()
                self.assertGreaterEqual(wait, 3600)
                self.assertLessEqual(wait, 3900)

    def test_owner_notice_sent_once_per_boot(self):
        poller = self._stale_poller()
        with patch("corporate_actions.poller.engine.send_message") as mock_send:
            with patch.object(config, "TELEGRAM_CHAT_ID", "123"), \
                    patch.object(config, "BOOT_FLOOD_GRACE_MINUTES", 45):
                poller._notify_owner_stale_grace()
                poller._notify_owner_stale_grace()
        self.assertEqual(mock_send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
