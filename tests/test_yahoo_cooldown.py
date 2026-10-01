import time
import unittest
from unittest.mock import patch

from corporate_actions.sources import http as http_mod


class YahooCooldownTests(unittest.TestCase):
    def setUp(self):
        http_mod._yahoo_cooldown_until = 0.0

    def tearDown(self):
        http_mod._yahoo_cooldown_until = 0.0

    def test_note_sets_future_cooldown(self):
        with patch.object(http_mod, "_YAHOO_COOLDOWN_DEFAULT", 0.05), \
                patch.object(http_mod, "_YAHOO_COOLDOWN_MAX", 60.0):
            before = time.time()
            http_mod._note_yahoo_429(None)
            self.assertGreater(http_mod._yahoo_cooldown_until, before)

    def test_retry_after_honored_and_capped(self):
        with patch.object(http_mod, "_YAHOO_COOLDOWN_DEFAULT", 0.05), \
                patch.object(http_mod, "_YAHOO_COOLDOWN_MAX", 60.0):
            http_mod._note_yahoo_429("120")
            remaining = http_mod._yahoo_cooldown_until - time.time()
            self.assertLessEqual(remaining, 60.0)
            self.assertGreater(remaining, 50.0)

    def test_invalid_retry_after_falls_back_to_default(self):
        with patch.object(http_mod, "_YAHOO_COOLDOWN_DEFAULT", 0.05), \
                patch.object(http_mod, "_YAHOO_COOLDOWN_MAX", 60.0):
            http_mod._note_yahoo_429("not-a-number")
            remaining = http_mod._yahoo_cooldown_until - time.time()
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 1.0)

    def test_sleep_returns_immediately_when_expired(self):
        started = time.time()
        http_mod._yahoo_cooldown_sleep()
        self.assertLess(time.time() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
