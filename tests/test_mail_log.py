import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from corporate_actions import config
from corporate_actions.email import client
from corporate_actions.storage import maillog


def _patch_log_to_tmp(testcase):
    tmp = Path(tempfile.mkdtemp()) / "mail_log.json"
    patcher = patch.object(config, "MAIL_LOG_FILE", tmp)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return tmp


def _smtp_env():
    return (
        patch.object(config, "SMTP_HOST", "smtp.example.com"),
        patch.object(config, "SMTP_PORT", 587),
        patch.object(config, "SMTP_USER", "bot@example.com"),
        patch.object(config, "SMTP_PASS", "secret"),
        patch.object(config, "SMTP_FROM", ""),
        patch.object(config, "RESEND_API_KEY", ""),
    )


class MailLogTests(unittest.TestCase):
    def test_record_and_load_newest_first(self):
        _patch_log_to_tmp(self)
        maillog.record_mail("111", "both", "a@b.com", "subj1", True, "")
        maillog.record_mail("111", "open", "a@b.com", "subj2", False, "boom")
        entries = maillog.load_mail_log()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["subject"], "subj2")
        self.assertFalse(entries[0]["ok"])
        self.assertEqual(entries[0]["info"], "boom")
        self.assertTrue(entries[1]["ok"])

    def test_trimmed_to_max_entries(self):
        _patch_log_to_tmp(self)
        for i in range(maillog.MAX_ENTRIES + 10):
            maillog.record_mail("1", "test", "a@b.com", f"s{i}", True, "")
        self.assertEqual(len(maillog.load_mail_log(limit=100)), maillog.MAX_ENTRIES)

    def test_send_email_logs_success(self):
        _patch_log_to_tmp(self)
        patches = _smtp_env()
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        server = MagicMock()
        server.__enter__.return_value = server
        with patch.object(client.smtplib, "SMTP", return_value=server):
            ok, _ = client.send_email("a@b.com", "Report", ["<b>hi</b>"],
                                      kind="both", chat_id="111")
        self.assertTrue(ok)
        entries = maillog.load_mail_log()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "both")
        self.assertEqual(entries[0]["chat"], "111")
        self.assertTrue(entries[0]["ok"])

    def test_send_email_logs_failure(self):
        _patch_log_to_tmp(self)
        patches = _smtp_env()
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        class FailSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                raise OSError(101, "Network is unreachable")

            def login(self, *args):
                pass

            def send_message(self, *args):
                raise OSError(101, "Network is unreachable")

        with patch.object(client.smtplib, "SMTP", FailSMTP), \
                patch.object(client.smtplib, "SMTP_SSL", FailSMTP):
            ok, error = client.send_email("a@b.com", "Report", ["x"],
                                         kind="open", chat_id="222")
        self.assertFalse(ok)
        entries = maillog.load_mail_log()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["ok"])
        self.assertIn("unreachable", entries[0]["info"])

    def test_never_raises_on_broken_disk(self):
        with patch.object(maillog, "write_json", side_effect=OSError("disk full")), \
                patch.object(maillog, "read_json", side_effect=OSError("gone")):
            maillog.record_mail("1", "x", "a@b.com", "s", True, "")  # must not raise
            self.assertEqual(maillog.load_mail_log(), [])


if __name__ == "__main__":
    unittest.main()
