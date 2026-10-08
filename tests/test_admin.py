"""Tests for corporate_actions.admin: gate + user/schedule/toggle operations."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corporate_actions import admin, config


def _point_state_to_temp():
    """Point every state file at a fresh temp dir."""
    tmp = Path(tempfile.mkdtemp())
    data = tmp / "data"
    data.mkdir()
    return (
        patch.object(config, "WATCHLIST_FILE", data / "watchlist.json"),
        patch.object(config, "SUBSCRIPTIONS_FILE", data / "subscriptions.json"),
        patch.object(config, "SETTINGS_FILE", data / "settings.json"),
        patch.object(config, "SCHEDULE_FILE", data / "schedule.json"),
        patch.object(config, "SEEN_FILE", data / "seen_actions.json"),
    )


class AdminGateTests(unittest.TestCase):
    def setUp(self):
        cleanup = patch.dict(admin._sessions, {}, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)

    def test_disabled_when_admin_key_unset(self):
        with patch.dict(os.environ, {"ADMIN_KEY": ""}, clear=False):
            os.environ.pop("ADMIN_KEY", None)
            self.assertFalse(admin.is_enabled())
            self.assertEqual(
                admin.check_token("anything"),
                "admin area is disabled (set ADMIN_KEY on the host)",
            )

    def test_create_and_verify_session(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "s3cret"}):
            self.assertTrue(admin.is_enabled())
            token = admin.create_session("s3cret")
            self.assertIsInstance(token, str)
            self.assertTrue(admin.verify_session(token))
            self.assertEqual(admin.check_token(token), "")
            self.assertIn("invalid", admin.check_token("wrong-token"))

    def test_wrong_key_rejected(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "s3cret"}):
            self.assertIsNone(admin.create_session("nope"))
            self.assertIsNone(admin.create_session(""))

    def test_logout_ends_session(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "s3cret"}):
            token = admin.create_session("s3cret")
            admin.end_session(token)
            self.assertFalse(admin.verify_session(token))

    def test_unknown_token_rejected(self):
        self.assertFalse(admin.verify_session("never-issued"))
        self.assertFalse(admin.verify_session(""))


class AdminOpsTests(unittest.TestCase):
    def setUp(self):
        cleanup = patch.dict(admin._sessions, {}, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)
        for p in _point_state_to_temp():
            p.start()
            self.addCleanup(p.stop)
        self.owner = "862087765"
        tp = patch.object(config, "TELEGRAM_CHAT_ID", self.owner)
        tp.start()
        self.addCleanup(tp.stop)

    # ------------------------------------------------------ users ----

    def test_list_users_merges_settings_and_subscriptions(self):
        storage = admin.storage
        storage.save_user_settings(self.owner, {"email": "o@x.com", "ca_alerts": False})
        storage.add_subscription("6090749599", {"symbol": "ITC", "exchange": "NSE"})
        users = admin.list_users()
        self.assertIn(self.owner, users)
        self.assertIn("6090749599", users)
        self.assertTrue(users[self.owner]["is_owner"])
        self.assertFalse(users["6090749599"]["is_owner"])
        self.assertEqual(users[self.owner]["email"], "o@x.com")
        self.assertFalse(users[self.owner]["ca_alerts"])
        self.assertEqual(users["6090749599"]["list_count"], 1)

    def test_owner_list_is_watchlist(self):
        storage = admin.storage
        storage.add_to_watchlist([{"symbol": "RELIANCE", "exchange": "NSE"}])
        storage.add_subscription("6090749599", {"symbol": "ITC", "exchange": "NSE"})
        self.assertEqual([i["symbol"] for i in admin.user_list(self.owner)], ["RELIANCE"])
        self.assertEqual([i["symbol"] for i in admin.user_list("6090749599")], ["ITC"])

    def test_add_and_remove_user_symbols(self):
        result = admin.add_user_symbols(
            "6090749599", [{"symbol": "ITC", "company": "ITC Limited", "exchange": "NSE"}])
        self.assertEqual(result["added"], 1)
        # duplicate add is skipped
        result = admin.add_user_symbols(
            "6090749599", [{"symbol": "ITC", "company": "ITC Limited", "exchange": "NSE"}])
        self.assertEqual(result["added"], 0)
        remaining = admin.remove_user_symbol("6090749599", "itc", "NSE")
        self.assertEqual(remaining, [])

    def test_invalid_chat_rejected(self):
        with self.assertRaises(ValueError):
            admin.user_list("bad chat id!")

    # --------------------------------------------------- schedule ----

    def test_schedule_add_remove_clear(self):
        sched = admin.add_schedule(self.owner, 60, ["/movers"], run_at="09:00", market="in")
        self.assertEqual(len(sched), 1)
        self.assertEqual(sched[0]["commands"], ["/movers"])
        self.assertEqual(sched[0]["run_at"], "09:00")
        self.assertEqual(sched[0]["market"], "in")
        # other chats don't see it
        self.assertEqual(admin.user_schedule("6090749599"), [])
        sched = admin.remove_schedule(self.owner, 0)
        self.assertEqual(sched, [])
        admin.add_schedule(self.owner, 30, ["movers"])  # bare command gets the slash
        sched = admin.add_schedule(self.owner, 15, ["/corpactionsformylist"])
        self.assertEqual(len(sched), 2)
        sched = admin.clear_schedule(self.owner)
        self.assertEqual(sched, [])

    def test_add_schedule_validates(self):
        with self.assertRaises(ValueError):
            admin.add_schedule(self.owner, 0, ["/movers"])
        with self.assertRaises(ValueError):
            admin.add_schedule(self.owner, 60, ["   "])

    def test_pause_and_resume_schedule(self):
        admin.add_schedule(self.owner, 60, ["/movers"])
        sched = admin.pause_schedule(self.owner, 2)
        self.assertTrue(sched[0].get("paused_until"))
        sched = admin.resume_schedule(self.owner)
        self.assertIsNone(sched[0].get("paused_until"))

    # ---------------------------------------------------- toggles ----

    def test_master_alert_toggle_uses_quiet(self):
        state = admin.set_alerts_enabled(self.owner, False)
        self.assertTrue(state["quiet"])
        # storage-level check: the poller gates on the same flag
        self.assertTrue(admin.storage.is_quiet(self.owner))
        state = admin.set_alerts_enabled(self.owner, True)
        self.assertFalse(state["quiet"])
        self.assertFalse(admin.storage.is_quiet(self.owner))

    def test_schedule_suggestions_are_all_known_commands(self):
        from corporate_actions.bot.registry import is_known_command

        suggestions = admin.schedule_command_suggestions()
        self.assertTrue(len(suggestions) >= 10)
        for item in suggestions:
            self.assertTrue(item["value"].startswith("/"), item)
            self.assertTrue(item["hint"], item)
            self.assertTrue(is_known_command(item["value"]),
                            item["value"] + " should pass schedule validation")

    def test_ca_alert_toggle(self):
        state = admin.set_ca_alerts(self.owner, False)
        self.assertFalse(state["ca_alerts"])
        self.assertFalse(admin.storage.ca_alerts_enabled(self.owner))
        state = admin.set_ca_alerts(self.owner, True)
        self.assertTrue(state["ca_alerts"])
        self.assertTrue(admin.storage.ca_alerts_enabled(self.owner))


if __name__ == "__main__":
    unittest.main()
