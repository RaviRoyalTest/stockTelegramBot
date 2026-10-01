"""Email delivery package (SMTP, stdlib only).

Sends the bot's HTML reports to a user's mail id via any SMTP provider
(Gmail with an App Password, Outlook, etc.). No new dependencies:
smtplib + email.message from the standard library.
"""
from .client import EmailError, is_configured, send_custom, send_email
from .client import GMAIL_SETUP_GUIDE, parse_recipients, provider_name, status
from .client import text_to_html_lines, with_prefix
from .daily import build_daily_lines, maybe_send_daily_email
from .resend import is_configured as resend_configured
from .resend import send_via_resend

__all__ = [
    "EmailError",
    "GMAIL_SETUP_GUIDE",
    "is_configured",
    "parse_recipients",
    "provider_name",
    "send_custom",
    "send_email",
    "status",
    "text_to_html_lines",
    "with_prefix",
    "build_daily_lines",
    "maybe_send_daily_email",
    "resend_configured",
    "send_via_resend",
]
