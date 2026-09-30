import smtplib
import unittest
from unittest.mock import MagicMock, patch

from corporate_actions import config
from corporate_actions.email import client


def _env(host="smtp.example.com", port=587):
    return patch.object(config, "SMTP_HOST", host), \
        patch.object(config, "SMTP_PORT", port), \
        patch.object(config, "SMTP_USER", "bot@example.com"), \
        patch.object(config, "SMTP_PASS", "secret"), \
        patch.object(config, "SMTP_FROM", "")


class EmailClientTests(unittest.TestCase):
    def test_invalid_recipient_rejected_without_network(self):
        ok, err = client.send_email("not-an-address", "s", ["x"])
        self.assertFalse(ok)
        self.assertIn("invalid recipient", err)

    def test_unconfigured_server_reported(self):
        with patch.object(config, "SMTP_HOST", ""), \
                patch.object(config, "SMTP_USER", ""), \
                patch.object(config, "SMTP_PASS", ""):
            ok, err = client.send_email("a@b.com", "s", ["x"])
        self.assertFalse(ok)
        self.assertIn("not configured", err)

    def test_network_failure_names_endpoint_and_falls_back(self):
        host, port, user, pwd, frm = _env()
        calls = []

        class FailSMTP:
            def __init__(self, *args, **kwargs):
                calls.append(args[1])  # port attempted
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

        with host, port, user, pwd, frm, \
                patch.object(client.smtplib, "SMTP", FailSMTP), \
                patch.object(client.smtplib, "SMTP_SSL", FailSMTP):
            ok, err = client.send_email("a@b.com", "s", ["x"])
        self.assertFalse(ok)
        # Configured 587 tried first, then the 465 fallback.
        self.assertEqual(calls, [587, 465])
        self.assertIn("smtp.example.com:587 unreachable", err)
        self.assertIn("smtp.example.com:465 unreachable", err)
        self.assertIn("Network is unreachable", err)

    def test_auth_failure_stops_without_fallback(self):
        host, port, user, pwd, frm = _env()
        calls = []

        class AuthFailSMTP:
            def __init__(self, *args, **kwargs):
                calls.append(args[1])
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def starttls(self):
                pass
            def login(self, *args):
                raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
            def send_message(self, *args):
                pass

        with host, port, user, pwd, frm, \
                patch.object(client.smtplib, "SMTP", AuthFailSMTP):
            ok, err = client.send_email("a@b.com", "s", ["x"])
        self.assertFalse(ok)
        self.assertEqual(calls, [587])
        self.assertIn("rejected the login", err)

    def test_successful_send(self):
        host, port, user, pwd, frm = _env()
        server = MagicMock()
        server.__enter__.return_value = server
        with host, port, user, pwd, frm, \
                patch.object(client.smtplib, "SMTP", return_value=server):
            ok, err = client.send_email("a@b.com", "subject", ["<b>hi</b>"])
        self.assertTrue(ok)
        self.assertEqual(err, "")
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("bot@example.com", "secret")
        server.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
