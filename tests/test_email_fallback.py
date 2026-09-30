"""Regression tests: Resend failure must fall back to SMTP when available."""
import unittest
import unittest.mock as mock
from unittest.mock import patch

from corporate_actions import config
from corporate_actions.email import client, resend as resend_mod


class ResendFallbackTests(unittest.TestCase):
    def test_resend_failure_falls_back_to_smtp_and_succeeds(self):
        server = mock.MagicMock()
        server.__enter__.return_value = server
        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(resend_mod, "send_via_resend",
                             return_value=(False, "resend rejected the request (HTTP 403: domain not verified)")), \
                patch.object(config, "SMTP_HOST", "smtp.gmail.com"), \
                patch.object(config, "SMTP_PORT", 587), \
                patch.object(config, "SMTP_USER", "bot@gmail.com"), \
                patch.object(config, "SMTP_PASS", "app-password"), \
                patch.object(config, "SMTP_FROM", ""), \
                patch.object(client.smtplib, "SMTP", return_value=server):
            ok, err = client.send_email("a@b.com", "subj", ["<b>hi</b>"])
        self.assertTrue(ok)
        self.assertEqual(err, "")
        server.send_message.assert_called_once()

    def test_resend_failure_without_smtp_reports_resend_error(self):
        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(resend_mod, "send_via_resend",
                             return_value=(False, "resend rejected the request (HTTP 401: bad key)")), \
                patch.object(config, "SMTP_HOST", ""), \
                patch.object(config, "SMTP_USER", ""), \
                patch.object(config, "SMTP_PASS", ""):
            ok, err = client.send_email("a@b.com", "subj", ["x"])
        self.assertFalse(ok)
        self.assertIn("HTTP 401", err)
        self.assertNotIn("fallback", err)

    def test_resend_and_smtp_both_failing_reports_both(self):
        server = mock.MagicMock()
        server.__enter__.return_value = server
        server.starttls.side_effect = OSError(111, "Connection refused")
        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(resend_mod, "send_via_resend",
                             return_value=(False, "resend rejected the request (HTTP 403: nope)")), \
                patch.object(config, "SMTP_HOST", "smtp.gmail.com"), \
                patch.object(config, "SMTP_PORT", 587), \
                patch.object(config, "SMTP_USER", "bot@gmail.com"), \
                patch.object(config, "SMTP_PASS", "app-password"), \
                patch.object(config, "SMTP_FROM", ""), \
                patch.object(client.smtplib, "SMTP", return_value=server):
            ok, err = client.send_email("a@b.com", "subj", ["x"])
        self.assertFalse(ok)
        self.assertIn("resend rejected", err)
        self.assertIn("smtp fallback also failed", err)

    def test_resend_success_does_not_touch_smtp(self):
        smtp = mock.MagicMock()
        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(resend_mod, "send_via_resend", return_value=(True, "")), \
                patch.object(config, "SMTP_HOST", "smtp.gmail.com"), \
                patch.object(config, "SMTP_USER", "bot@gmail.com"), \
                patch.object(config, "SMTP_PASS", "app-password"), \
                patch.object(client.smtplib, "SMTP", smtp):
            ok, err = client.send_email("a@b.com", "subj", ["x"])
        self.assertTrue(ok)
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
