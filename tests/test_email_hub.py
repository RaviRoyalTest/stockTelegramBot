"""Bare /email hub: one command that shows the mailbox setup + every mail command.

Covers the user request: a single command (like /email) should show only the
email category's details and enumerate all possible commands in that category,
consistently across the Telegram registry, /emailhelp, the README and the
dashboard.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corporate_actions import config
from corporate_actions.bot import dispatch
from corporate_actions.bot import email_commands
from corporate_actions.bot import help_texts
from corporate_actions.bot import registry


EMAIL_COMMANDS = (
    "/setemail", "/email", "/emailreport", "/dailyemail", "/emailsections",
    "/emailopen", "/emailclose", "/emailboth", "/emailstatus", "/emailhelp",
)


class EmailHubTests(unittest.TestCase):
    def setUp(self):
        # Dispatching a command records it in the recent-commands list, so the
        # settings file must be redirected - a test run must never add a fake
        # chat to the committed data/settings.json.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(config, "SETTINGS_FILE",
                               Path(tmp.name) / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_overview_lists_every_email_command(self):
        overview = email_commands.EMAIL_COMMANDS_OVERVIEW
        self.assertTrue(len(overview) >= 9)
        for cmd in EMAIL_COMMANDS:
            self.assertTrue(
                any(cmd in line for line in overview),
                f"{cmd} missing from EMAIL_COMMANDS_OVERVIEW",
            )

    def test_email_usage_built_from_overview(self):
        for cmd in EMAIL_COMMANDS:
            self.assertIn(cmd, email_commands.EMAIL_USAGE)

    def test_all_commands_text_mentions_email_hub(self):
        self.assertIn("/email", help_texts.ALL_COMMANDS_TEXT)
        self.assertIn("hub", help_texts.ALL_COMMANDS_TEXT.lower())

    def test_help_text_mentions_email_hub(self):
        self.assertIn("/email", help_texts.HELP_TEXT)

    def test_bare_email_shows_status_and_all_commands(self):
        settings = {
            "email": "raviroyalmechai@gmail.com",
            "daily_email": True,
            "email_scope": "both",
        }
        captured = {}

        def fake_reply(chat_id, text, **_kw):
            captured["text"] = text

        with patch.object(registry, "reply", fake_reply), \
             patch.object(registry.storage, "get_user_settings", return_value=settings), \
             patch.object(registry.storage, "get_recent_commands", return_value=[]), \
             patch("corporate_actions.email.is_configured", return_value=True):
            ok = registry._bare_command_usage(123, "/email")
        self.assertTrue(ok)
        text = captured["text"]
        self.assertIn("Your mailbox", text)
        # The address is masked - domain shown, raw local part must NOT leak.
        self.assertIn("gmail.com", text)
        self.assertNotIn("raviroyalmechai", text)
        self.assertIn("Daily mails: <b>ON (both)</b>", text)
        for cmd in EMAIL_COMMANDS:
            self.assertIn(cmd, text)

    def test_bare_email_alias_also_shows_hub(self):
        captured = {}

        def fake_reply(chat_id, text, **_kw):
            captured["text"] = text

        with patch.object(registry, "reply", fake_reply), \
             patch.object(registry.storage, "get_user_settings", return_value={}), \
             patch.object(registry.storage, "get_recent_commands", return_value=[]), \
             patch("corporate_actions.email.is_configured", return_value=False):
            dispatch.handle_command(123, "/emailsend")  # alias -> /email
        self.assertIn("Your mailbox", captured["text"])
        self.assertIn("NOT configured", captured["text"])

    def test_email_command_registered_in_menu(self):
        captured = {}

        def fake_set(menu):
            captured["menu"] = menu

        with patch.object(registry, "is_configured", return_value=True), \
             patch.object(registry, "set_my_commands", side_effect=fake_set):
            registry.register_commands()
        menu = {m["command"]: m["description"] for m in captured["menu"]}
        self.assertIn("email", menu)
        self.assertIn("/email", menu["email"])


if __name__ == "__main__":
    unittest.main()
