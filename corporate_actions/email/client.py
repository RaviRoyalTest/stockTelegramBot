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
    """True when any sender is usable (Resend key or full SMTP triple)."""
    from . import resend as resend_mod

    return resend_mod.is_configured() or bool(
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


def _deliver(host: str, port: int, user: str, password: str,
             message: EmailMessage) -> None:
    """One SMTP attempt (raises on any failure)."""
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as server:
            server.login(user, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(message)


def _smtp_attempt(recipient: str, subject: str,
                  html_lines: list[str]) -> tuple[bool, str]:
    """One full SMTP chain (with 465/587 port fallback). Never raises."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM or config.SMTP_USER
    message["To"] = recipient
    message.set_content(_plain_fallback(html_lines))
    message.add_alternative(_html_document(subject, html_lines), subtype="html")

    host = config.SMTP_HOST
    primary = config.SMTP_PORT
    alternates = [primary] + ([465, 587] if primary != 465 else [587])
    # de-dupe while keeping order (configured port first)
    ports: list[int] = []
    for port in alternates:
        if port not in ports:
            ports.append(port)

    failures: list[str] = []
    for port in ports:
        try:
            _deliver(host, port, config.SMTP_USER, config.SMTP_PASS, message)
        except smtplib.SMTPAuthenticationError as error:
            # Credentials rejected - another port will say the same; stop.
            detail = config.redact(str(error))
            log.warning("email to %s refused auth at %s:%s", recipient, host, port)
            return False, f"{host}:{port} rejected the login ({detail}) - check SMTP_USER/SMTP_PASS"
        except OSError as error:
            # Network-level: DNS, refused, timeout, unreachable. Try next port.
            failures.append(f"{host}:{port} unreachable ({config.redact(str(error))})")
            log.info("email via %s:%s failed, trying next port: %s", host, port, error)
            continue
        except Exception as error:
            detail = config.redact(str(error))
            log.warning("email to %s failed at %s:%s: %s", recipient, host, port, error)
            return False, f"{host}:{port} failed ({detail})"
        log.info("email sent to %s via %s:%s: %s", recipient, host, port, subject)
        return True, ""
    hint = (" Check SMTP_HOST spelling, outbound firewall, and that ports "
            "587/465 are allowed from this host.")
    return False, "; ".join(failures) + "." + hint


def send_email(to: str, subject: str, html_lines: list[str]) -> tuple[bool, str]:
    """Send report lines to one mail id. Returns (ok, error_message).

    Transport priority: Resend HTTPS API when RESEND_API_KEY is set (works
    on hosts where SMTP ports are blocked), with the SMTP chain as a
    fallback when Resend fails AND full SMTP credentials exist. Without a
    Resend key the SMTP chain runs directly; its errors name host:port.
    """
    recipient = (to or "").strip()
    if not recipient or "@" not in recipient:
        return False, "invalid recipient address"
    if not is_configured():
        return False, (
            "email is not configured on the server "
            "(set RESEND_API_KEY, or SMTP_HOST/SMTP_USER/SMTP_PASS)"
        )
    from . import resend as resend_mod

    if resend_mod.is_configured():
        ok, error = resend_mod.send_via_resend(
            recipient, subject,
            _html_document(subject, html_lines),
            _plain_fallback(html_lines),
        )
        if ok:
            return True, ""
        smtp_usable = bool(
            config.SMTP_HOST.strip()
            and config.SMTP_USER.strip()
            and config.SMTP_PASS
        )
        if not smtp_usable:
            return False, error
        log.warning(
            "resend failed (%s) - falling back to SMTP %s", error, config.SMTP_HOST
        )
        smtp_ok, smtp_error = _smtp_attempt(recipient, subject, html_lines)
        if smtp_ok:
            return True, ""
        return False, f"{error}; smtp fallback also failed ({smtp_error})"
    return _smtp_attempt(recipient, subject, html_lines)
