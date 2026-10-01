import smtplib
import tempfile
import unittest
from pathlib import Path
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
    def setUp(self):
        # Keep sends hermetic: the mail log must never touch data/mail_log.json.
        tmp = Path(tempfile.mkdtemp()) / "mail_log.json"
        self._log_patch = patch.object(config, "MAIL_LOG_FILE", tmp)
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)

    def test_invalid_recipient_rejected_without_network(self):
        ok, err = client.send_email("not-an-address", "s", ["x"])
        self.assertFalse(ok)
        self.assertIn("invalid recipient", err)

    def test_unconfigured_server_reported(self):
        with patch.object(config, "SMTP_HOST", ""), \
                patch.object(config, "SMTP_USER", ""), \
                patch.object(config, "SMTP_PASS", ""), \
                patch.object(config, "RESEND_API_KEY", ""):
            ok, err = client.send_email("a@b.com", "s", ["x"])
        self.assertFalse(ok)
        self.assertIn("not configured", err)

    def test_resend_preferred_when_key_present(self):
        from corporate_actions.email import resend as resend_mod

        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(resend_mod, "send_via_resend",
                             return_value=(True, "")) as sender, \
                patch.object(config, "SMTP_HOST", ""), \
                patch.object(config, "SMTP_USER", ""), \
                patch.object(config, "SMTP_PASS", ""):
            ok, err = client.send_email("a@b.com", "subj", ["<b>hi</b>"])
        self.assertTrue(ok)
        self.assertEqual(err, "")
        sender.assert_called_once()
        self.assertEqual(sender.call_args[0][0], "a@b.com")
        self.assertEqual(sender.call_args[0][1], "subj")

    def test_resend_http_error_reported(self):
        import urllib.error

        from corporate_actions.email import resend as resend_mod

        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(config, "RESEND_FROM", ""):
            fake_error = urllib.error.HTTPError(
                "https://api.resend.com/emails", 401, "Unauthorized", {}, None)
            with patch("urllib.request.urlopen", side_effect=fake_error):
                ok, err = resend_mod.send_via_resend(
                    "a@b.com", "s", "<b>hi</b>", "hi")
        self.assertFalse(ok)
        self.assertIn("HTTP 401", err)

    def test_resend_success(self):
        from corporate_actions.email import resend as resend_mod

        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"id":"abc"}'
        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(config, "RESEND_FROM", ""):
            with patch("urllib.request.urlopen", return_value=response) as opener:
                ok, err = resend_mod.send_via_resend(
                    "a@b.com", "s", "<b>hi</b>", "hi")
        self.assertTrue(ok)
        # Success info carries the Resend message id (delivery tracking).
        self.assertEqual(err, "resend id: abc")
        request = opener.call_args[0][0]
        self.assertIn("api.resend.com/emails", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer re_test123")

    def test_resend_request_sends_explicit_user_agent(self):
        """Cloudflare blocks urllib's default UA (403 err 1010) - always send ours."""
        from corporate_actions.email import resend as resend_mod

        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"id":"abc"}'
        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(config, "RESEND_FROM", ""):
            with patch("urllib.request.urlopen", return_value=response) as opener:
                resend_mod.send_via_resend("a@b.com", "s", "<b>hi</b>", "hi")
        request = opener.call_args[0][0]
        # urllib capitalizes header keys, so "User-agent" is the lookup form.
        self.assertEqual(request.get_header("User-agent"), resend_mod.USER_AGENT)

    def test_resend_success_info_carries_message_id(self):
        """Success info carries the Resend message id for delivery tracking."""
        from corporate_actions.email import client as client_mod
        from corporate_actions.email import resend as resend_mod

        with patch.object(config, "RESEND_API_KEY", "re_test123"), \
                patch.object(config, "SMTP_HOST", ""), \
                patch.object(config, "SMTP_USER", ""), \
                patch.object(config, "SMTP_PASS", ""), \
                patch.object(resend_mod, "send_via_resend",
                             return_value=(True, "resend id: abc123")):
            ok, info = client_mod.send_email("a@b.com", "subj", ["<b>hi</b>"])
        self.assertTrue(ok)
        self.assertEqual(info, "resend id: abc123")

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
