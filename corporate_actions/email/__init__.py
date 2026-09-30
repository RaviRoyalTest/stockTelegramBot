"""Email delivery package (SMTP, stdlib only).

Sends the bot's HTML reports to a user's mail id via any SMTP provider
(Gmail with an App Password, Outlook, etc.). No new dependencies:
smtplib + email.message from the standard library.
"""
from .client import EmailError, is_configured, send_email
from .daily import build_daily_lines, maybe_send_daily_email
from .resend import is_configured as resend_configured
from .resend import send_via_resend

__all__ = [
    "EmailError",
    "is_configured",
    "send_email",
    "build_daily_lines",
    "maybe_send_daily_email",
    "resend_configured",
    "send_via_resend",
]
