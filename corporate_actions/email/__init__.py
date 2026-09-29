"""Email delivery package (SMTP, stdlib only).

Sends the bot's HTML reports to a user's mail id via any SMTP provider
(Gmail with an App Password, Outlook, etc.). No new dependencies:
smtplib + email.message from the standard library.
"""
from .client import EmailError, is_configured, send_email

__all__ = ["EmailError", "is_configured", "send_email"]
