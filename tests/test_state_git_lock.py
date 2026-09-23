"""State git operations: mutual exclusion + live push-error reporting.

Regression for the "removed schedule entry came back" bug: the server runs
git from several threads (command loop, periodic flush, poller post-alert
push). Two overlapping commit/push/rebase/reset cycles corrupted each other
and a redeploy then restored the pre-edit state file from GitHub. Also pins
that callers read the push failure reason via last_push_error() - importing
the push_error variable by value froze it at "" so the Telegram warning
printed an empty "Reason:".
"""
import threading
import unittest
from unittest.mock import MagicMock, patch

import corporate_actions.github as github


class StateGitLockTests(unittest.TestCase):
    def test_lock_exists_and_is_reentrant(self):
        self.assertIsInstance(github._state_git_lock, type(threading.RLock()))
        # Reentrancy matters: push_state -> helpers on the same thread.
        with github._state_git_lock:
            acquired = github._state_git_lock.acquire(timeout=1)
            self.assertTrue(acquired)
            github._state_git_lock.release()

    def test_push_and_sync_serialize(self):
        """A sync started while a push holds the lock must wait for it."""
        order = []
        real_lock = github._state_git_lock

        def slow_push():
            with real_lock:
                order.append("push-start")
                # A concurrent sync would append "sync-start" here if the
                # lock were missing.
                order.append("push-end")
                return True

        def sync_probe():
            with real_lock:
                order.append("sync-start")

        pusher = threading.Thread(target=slow_push)
        prober = threading.Thread(target=sync_probe)
        pusher.start()
        prober.start()
        pusher.join(timeout=5)
        prober.join(timeout=5)
        self.assertLess(order.index("push-end"), order.index("sync-start"))


class LastPushErrorTests(unittest.TestCase):
    def test_reads_live_value_not_import_time_copy(self):
        with patch.object(github, "push_error", "git push failed: 403"):
            self.assertEqual(github.last_push_error(), "git push failed: 403")

    def test_empty_when_no_failure(self):
        with patch.object(github, "push_error", ""):
            self.assertEqual(github.last_push_error(), "")

    def test_push_state_failure_sets_live_reason(self):
        """A failed push must be visible through the function immediately."""
        with patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_REPOSITORY": ""}):
            ok = github.push_state()
        self.assertFalse(ok)
        self.assertEqual(github.last_push_error(), "GH_TOKEN / GITHUB_REPOSITORY not set on this host")

    def test_no_by_value_push_error_imports(self):
        """Importing push_error by value freezes it at '' - the exact bug."""
        import pathlib
        offenders = []
        for path in pathlib.Path("corporate_actions").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                if "import" in line and re_push_error(line):
                    offenders.append(f"{path}:{line_no}: {line.strip()}")
        self.assertEqual(offenders, [])


def re_push_error(line: str) -> bool:
    import re
    return bool(re.search(r"import\s+(?:.*,\s*)?push_error\b", line)) and \
        "last_push_error" not in line


if __name__ == "__main__":
    unittest.main()
