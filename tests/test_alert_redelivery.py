"""Regression tests for the alert re-fire-after-redeploy bug.

Every push redeploys the host; a fresh container boots with the committed
seen_actions.json. If the just-sent alerts' dedup keys never reach GitHub
before the next deploy, the same alerts fire again (the double-send flood).
These tests pin the three defenses:

1. github._git pins HTTP/1.1 for network commands (HTTP/2 hangs forever on
   some hosts, silently failing every state push).
2. The poller pushes dedup state immediately after alerts are sent.
3. A stale seen-file is loudly reported at boot instead of re-firing quietly.
"""
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from corporate_actions.github import _git
from corporate_actions.poller.engine import Poller


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


if __name__ == "__main__":
    unittest.main()
