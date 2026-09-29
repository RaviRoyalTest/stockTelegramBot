"""SMTP email client for sending bot reports to a mail id.

Configuration (environment, never committed):
  SMTP_HOST  e.g. smtp.gmail.com
  SMTP_PORT  e.g. 587 (STARTTLS) or 465 (SSL)
  SMTP_USER  sender address / login
  SMTP_PASS  password (Gmail: an App Password, not the login password)
  SMTP_FROM  display sender (defaults to SMTP_USER)

Never raises for delivery problems - send_email returns (ok, error) so
command handlers can reply the outcome in chat instead of crashing.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .. import config

log = logging.getLogger(__name__)


class EmailError(Exception):
    """SMTP delivery failure (bad credentials, unreachable host, rejected)."""


def is_configured() -> bool:
    """True when the SMTP sender settings are all present."""
    return bool(
        config.SMTP_HOST.strip()
        and config.SMTP_USER.strip()
        and config.SMTP_PASS
    )


def _html_document(title: str, body_lines: list[str]) -> str:
    """Wrap Telegram-style HTML report lines in a minimal email document."""
    body = "<br>\n".join(body_lines)
    return (
        "<!DOCTYPE html><html><body style=\"font-family:Arial,sans-serif;"
        "font-size:14px;color:#111;\">"
        f"<h2>{title}</h2><div>{body}</div>"
        "<hr><p style=\"color:#888;font-size:12px;\">Sent by Royal Stock bot</p>"
        "</body></html>"
    )


def _plain_fallback(html_lines: list[str]) -> str:
    """Best-effort text version (tags stripped) for plain-text clients."""
    import re

    text = "\n".join(html_lines)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def send_email(to: str, subject: str, html_lines: list[str]) -> tuple[bool, str]:
    """Send report lines to one mail id. Returns (ok, error_message)."""
    recipient = (to or "").strip()
    if not recipient or "@" not in recipient:
        return False, "invalid recipient address"
    if not is_configured():
        return False, (
            "email is not configured on the server "
            "(SMTP_HOST/SMTP_USER/SMTP_PASS missing)"
        )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM or config.SMTP_USER
    message["To"] = recipient
    message.set_content(_plain_fallback(html_lines))
    message.add_alternative(_html_document(subject, html_lines), subtype="html")
    port = config.SMTP_PORT
    try:
        if port == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, port, timeout=20) as server:
                server.login(config.SMTP_USER, config.SMTP_PASS)
                server.send_message(message)
        else:
            with smtplib.SMTP(config.SMTP_HOST, port, timeout=20) as server:
                server.starttls()
                server.login(config.SMTP_USER, config.SMTP_PASS)
                server.send_message(message)
    except Exception as error:
        log.warning("email to %s failed: %s", recipient, error)
        return False, config.redact(str(error))
    log.info("email sent to %s: %s", recipient, subject)
    return True, ""
