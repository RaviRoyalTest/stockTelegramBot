"""Settings writers must preserve concurrent updates and explicit removals."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corporate_actions import admin, config, storage
from corporate_actions.bot import email_commands as email
from corporate_actions.bot import settings_commands as commands
from corporate_actions.email import daily
from corporate_actions.formatting import schedule as formatting
from corporate_actions.storage import settings as settings_store


class SettingsUpdateIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in (
            ("SETTINGS_FILE", root / "settings.json"),
            ("SCHEDULE_FILE", root / "schedule.json"),
            ("TELEGRAM_CHAT_ID", "123"),
        ):
            context = patch.object(config, name, value)
            context.start()
            self.addCleanup(context.stop)
        for module in (commands, email):
            context = patch.object(module, "reply")
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(storage, "save_user_settings", side_effect=AssertionError(
            "External settings writers must not replace a stale settings object"
        ))
        context.start()
        self.addCleanup(context.stop)

    def seed(self, values):
        settings_store.patch_user_settings("123", values)

    def current(self):
        return settings_store.get_user_settings("123")

    def test_top_level_commands_preserve_interleaved_settings(self):
        cases = [
            (commands.handle_alertfilters, ["/filter", "off"], {"ca_alerts": False}),
            (commands.handle_alertfilters, ["/filter", "on"], {"ca_alerts": True}),
            (commands.handle_alertfilters, ["/filter", "all"], {"action_filters": []}),
            (commands.handle_alertfilters, ["/filter", "dividend"], {"action_filters": ["dividend"]}),
            (commands.handle_corpaction_alerts, ["/corpactions", "on"], {"ca_alerts": True}),
            (commands.handle_corpaction_alerts, ["/corpactions", "off"], {"ca_alerts": False}),
            (commands.handle_pricealert, ["/alert", "3"], {"price_alert_pct": 3, "last_price_alert_pct": 3}),
            (commands.handle_pricealert, ["/alert", "off"], {"price_alert_pct": None}),
            (commands.handle_moversfund, ["/fundmode", "auto"], {"movers_fund": "auto"}),
            (commands.handle_moversfund, ["/fundmode", "button"], {"movers_fund": "button"}),
            (commands.handle_quiet, ["/quiet", "on"], {"quiet": True}),
            (commands.handle_quiet, ["/quiet", "off"], {"quiet": False}),
            (commands.handle_quiet, ["/quiet", "2h"], {"quiet": True}),
            (email.handle_setemail, ["/setemail", "new@example.com"], {"email": "new@example.com"}),
            (email.handle_setemail, ["/setemail", "off"], {}),
            (email.handle_dailyemail, ["/dailyemail", "on"], {"daily_email": True, "email_scope": "both"}),
            (email.handle_dailyemail, ["/dailyemail", "open"], {"email_scope": "open"}),
            (email.handle_dailyemail, ["/dailyemail", "close"], {"email_scope": "close"}),
            (email.handle_dailyemail, ["/dailyemail", "scheduled"], {"email_scope": "scheduled"}),
            (email.handle_dailyemail, ["/dailyemail", "off"], {"daily_email": False, "email_scope": "off"}),
            (email.handle_emailsections, ["/emailsections", "nifty,gappers"], {"email_sections": ["nifty", "gappers"]}),
            (email.handle_emailsections, ["/emailsections", "close", "nifty,eod"], {"email_close_sections": ["nifty", "eod"]}),
        ]
        for handler, parts, expected in cases:
            with self.subTest(parts=parts):
                self.seed({"email": "old@example.com", "unrelated": "old", "last_price_alert_pct": 7})

                def interleave(chat, changes, *, remove=()):
                    settings_store.patch_user_settings(chat, {
                        "unrelated": "new", "recent_commands": ["/news"],
                    })
                    return settings_store.patch_user_settings(chat, changes, remove=remove)

                with patch.object(storage, "patch_user_settings", side_effect=interleave) as writer, \
                        patch.object(email, "email_configured", return_value=True), \
                        patch.object(email, "send_email", return_value=(True, "")):
                    handler("123", parts)
                writer.assert_called_once()
                saved = self.current()
                self.assertEqual(saved["unrelated"], "new")
                self.assertEqual(saved["recent_commands"], ["/news"])
                for key, value in expected.items():
                    self.assertEqual(saved[key], value)

    def test_removals_are_explicit_and_persist(self):
        cases = [
            (lambda: admin.set_alerts_enabled("123", True), ("quiet", "quiet_until")),
            (lambda: admin.set_alerts_enabled("123", False), ("quiet_until",)),
            (lambda: commands.handle_quiet("123", ["/quiet", "off"]), ("quiet_until",)),
            (lambda: commands.handle_quiet("123", ["/quiet", "on"]), ("quiet_until",)),
            (lambda: email.handle_setemail("123", ["/setemail", "off"]), ("email",)),
        ]
        for invoke, removed in cases:
            with self.subTest(removed=removed, invoke=invoke):
                self.seed({"quiet": True, "quiet_until": 9999999999, "email": "old@example.com"})
                with patch.object(storage, "patch_user_settings", wraps=settings_store.patch_user_settings) as writer:
                    invoke()
                self.assertEqual(writer.call_args.kwargs["remove"], removed)
                for key in removed:
                    self.assertNotIn(key, self.current())

    def test_admin_toggles_preserve_unrelated_updates(self):
        for invoke, expected in (
            (lambda: admin.set_alerts_enabled("123", False), {"quiet": True}),
            (lambda: admin.set_alerts_enabled("123", True), {"quiet": False}),
            (lambda: admin.set_ca_alerts("123", False), {"ca_alerts": False}),
            (lambda: admin.set_ca_alerts("123", True), {"ca_alerts": True}),
        ):
            with self.subTest(expected=expected):
                self.seed({"email": "old@example.com"})

                def interleave(chat, changes, *, remove=()):
                    settings_store.patch_user_settings(chat, {"email": "new@example.com"})
                    return settings_store.patch_user_settings(chat, changes, remove=remove)

                with patch.object(storage, "patch_user_settings", side_effect=interleave):
                    state = invoke()
                self.assertEqual(state["email"], "new@example.com")
                for key, value in expected.items():
                    self.assertEqual(state[key], value)

    def test_nested_alert_edits_use_latest_configuration(self):
        cases = [
            (commands.handle_watcher, ["/watcher", "on"], "watcher", {"enabled": True}),
            (commands.handle_watcher, ["/watcher", "off"], "watcher", {"enabled": False}),
            (commands.handle_watcher, ["/watcher", "set", "3"], "watcher", {"threshold": 3}),
            (commands.handle_watcher, ["/watcher", "set", "3", "mylist"], "watcher", {"threshold": 3, "universe": "mylist"}),
            (commands.handle_watcher, ["/watcher", "universe", "mylist"], "watcher", {"universe": "mylist"}),
            (commands.handle_watcher, ["/watcher", "universe", "mylist", "4"], "watcher", {"universe": "mylist", "threshold": 4}),
            (commands.handle_highlow, ["/highlow", "on"], "highlow", {"enabled": True}),
            (commands.handle_highlow, ["/highlow", "off"], "highlow", {"enabled": False}),
            (commands.handle_highlow, ["/highlow", "universe", "mylist"], "highlow", {"universe": "mylist"}),
        ]
        for handler, parts, key, changes in cases:
            with self.subTest(parts=parts):
                self.seed({key: {"enabled": False, "universe": "nifty100", "threshold": 5}})
                latest = {"enabled": True, "universe": "nifty500", "threshold": 9, "future_field": "keep"}

                def interleave(chat, callback):
                    settings_store.patch_user_settings(chat, {key: latest, "email": "new@example.com"})
                    return settings_store.update_user_settings(chat, callback)

                with patch.object(storage, "update_user_settings", side_effect=interleave) as writer:
                    handler("123", parts)
                writer.assert_called_once()
                self.assertEqual(self.current()[key], dict(latest, **changes))
                self.assertEqual(self.current()["email"], "new@example.com")

    def test_price_alert_restore_uses_latest_threshold(self):
        self.seed({"last_price_alert_pct": 3})

        def interleave(chat, callback):
            settings_store.patch_user_settings(chat, {"last_price_alert_pct": 9, "email": "new@example.com"})
            return settings_store.update_user_settings(chat, callback)

        with patch.object(storage, "update_user_settings", side_effect=interleave):
            commands.handle_pricealert("123", ["/alert", "on"])
        self.assertEqual(self.current()["price_alert_pct"], 9)
        self.assertEqual(self.current()["email"], "new@example.com")

    def test_email_section_add_remove_uses_latest_list(self):
        for kind, key in (("morning", "email_sections"), ("close", "email_close_sections")):
            for action, expected in (("add", ["nifty", "us", "gappers"] if kind == "morning" else ["nifty", "us"]),
                                     ("remove", ["us"])):
                with self.subTest(kind=kind, action=action):
                    self.seed({key: ["nifty"]})

                    def interleave(chat, callback):
                        settings_store.patch_user_settings(chat, {key: ["nifty", "us"], "quiet": True})
                        return settings_store.update_user_settings(chat, callback)

                    word = ("gappers" if kind == "morning" else "us") if action == "add" else "nifty"
                    parts = ["/emailsections"] + (["close"] if kind == "close" else []) + [action, word]
                    with patch.object(storage, "update_user_settings", side_effect=interleave):
                        email.handle_emailsections("123", parts)
                    self.assertEqual(self.current()[key], expected)
                    self.assertTrue(self.current()["quiet"])

    def test_email_fallbacks_request_india_snapshots(self):
        self.seed({"email": "user@example.com"})
        snapshot = {"market": "in", "session": "2026-10-23"}
        for handler in (email.handle_emailopen, email.handle_emailclose, email.handle_emailboth):
            with self.subTest(handler=handler.__name__), \
                    patch.object(email, "email_configured", return_value=True), \
                    patch.object(email, "send_email", return_value=(True, "")) as send, \
                    patch.object(daily, "_recorded_openclose", return_value=({}, "session")), \
                    patch.object(storage, "load_snapshots", return_value=snapshot) as load, \
                    patch.object(daily, "build_daily_lines", return_value=["India digest"]) as build, \
                    patch.object(daily, "_owner_chat", return_value="123"), \
                    patch.object(storage, "load_watchlist", return_value=[]), \
                    patch.object(daily, "fetch_watchlist_quotes", return_value=[]), \
                    patch.object(daily, "build_eod_store_lines", return_value=[]):
                handler("123", ["/email"])
            load.assert_called_once_with("in")
            build.assert_called_once_with(snapshot)
            self.assertEqual(send.call_args.args[2], ["India digest"])

    def test_cleared_owner_schedule_does_not_show_environment_defaults(self):
        with patch.object(config, "SCHEDULED_COMMANDS", ["/news"]):
            self.assertIn("env defaults", formatting.format_schedule("123"))
            storage.clear_schedule("123")
            rendered = formatting.format_schedule("123")
        self.assertNotIn("env defaults", rendered)
        self.assertNotIn("/news", rendered)
        self.assertIn("no automated reports", rendered)

    def test_schedule_renders_canonical_times_and_explicit_timezone(self):
        entry = {
            "chat": "123", "commands": ["/news"], "interval_min": 1440,
            "at_times": ["15:30", "9:15", "09:15"], "run_at": "08:00",
            "market": "in", "timezone": "America/New_York",
            "window_start": "09:00", "window_end": "16:00",
        }
        with patch.object(storage, "load_schedule_for", return_value=[entry]), \
                patch.object(storage, "schedule_next_due_ts", return_value=1234567890), \
                patch.object(formatting, "format_next_run", return_value="next clock") as next_run:
            rendered = formatting.format_schedule("123")
        self.assertIn("daily: /news at 09:15, 15:30 America/New_York", rendered)
        self.assertIn("window 09:00–16:00 America/New_York", rendered)
        self.assertNotIn("08:00", rendered)
        next_run.assert_called_once_with(1234567890, "America/New_York", "America/New_York")

    def test_schedule_retains_legacy_clock_times_and_market_timezone(self):
        entry = {"chat": "123", "commands": ["/news"], "interval_min": 1440,
                 "run_at": "09:15,15:30", "market": "in"}
        with patch.object(storage, "load_schedule_for", return_value=[entry]), \
                patch.object(storage, "schedule_next_due_ts", return_value=None):
            rendered = formatting.format_schedule("123")
        self.assertIn("daily: /news at 09:15, 15:30 IST", rendered)

    def test_interval_schedule_shows_explicit_timezone(self):
        entry = {"chat": "123", "commands": ["/news"], "interval_min": 60,
                 "market": "any", "timezone": "America/New_York"}
        with patch.object(storage, "load_schedule_for", return_value=[entry]), \
                patch.object(storage, "schedule_next_due_ts", return_value=None):
            rendered = formatting.format_schedule("123")
        self.assertIn("timezone America/New_York", rendered)


if __name__ == "__main__":
    unittest.main()
