"""SMTP email client for sending bot reports to a mail id.

Configuration (environment, never committed):
  SMTP_HOST  e.g. smtp.gmail.com
  SMTP_PORT  e.g. 587 (STARTTLS) or 465 (SSL)
  SMTP_USER  sender address / login
  SMTP_PASS  password (Gmail: an App Password, not the login password)
  SMTP_FROM  display sender (defaults to SMTP_USER)
  EMAIL_SUBJECT_PREFIX  optional "[Prefix] " prepended to every subject
  EMAIL_FROM_NAME  friendly sender name (default "Royal Stock")

Never raises for delivery problems - send_email returns (ok, error) so
command handlers can reply the outcome in chat instead of crashing.
"""
from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .. import config

log = logging.getLogger(__name__)

# Short Gmail App-Password guide shown in Telegram when mail is not working.
# Kept here (not in the command module) so bot + web + logs share one text.
GMAIL_SETUP_GUIDE = (
    "Gmail setup (2 min, once):\n"
    "1. Google Account > Security > turn ON 2-Step Verification.\n"
    "2. Search 'App passwords' > create one for 'Mail' > copy the 16-letter code.\n"
    "3. On the host set SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, "
    "SMTP_USER=you@gmail.com, SMTP_PASS=<16-letter code> (spaces are stripped automatically).\n"
    "Tip: the login password will NOT work - it must be the App Password. "
    "Easier alternative: set RESEND_API_KEY (resend.com, free) and skip SMTP entirely."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def provider_name() -> str:
    """Human-readable active sender: 'resend' | 'smtp' | 'none'."""
    from . import resend as resend_mod

    if resend_mod.is_configured():
        return "resend"
    if config.SMTP_HOST.strip() and config.SMTP_USER.strip() and config.SMTP_PASS:
        return "smtp"
    return "none"


def status() -> dict:
    """Serializable sender status for /api/email/status + diagnostics."""
    return {
        "configured": is_configured(),
        "provider": provider_name(),
        "smtp_host": config.SMTP_HOST,
        "smtp_port": config.SMTP_PORT,
        "smtp_user_set": bool(config.SMTP_USER.strip()),
        "resend_key_set": bool((config.RESEND_API_KEY or "").strip()),
        "subject_prefix": config.EMAIL_SUBJECT_PREFIX,
        "from_name": config.EMAIL_FROM_NAME,
    }


def parse_recipients(raw: str, limit: int = 5) -> tuple[list[str], list[str]]:
    """Split a free-form recipient string into (valid, invalid) addresses.

    Accepts comma/semicolon/whitespace separated mail ids, lower-cased and
    de-duplicated. At most `limit` recipients are kept (spam guard).
    """
    seen: list[str] = []
    invalid: list[str] = []
    for token in re.split(r"[\s,;]+", (raw or "").strip()):
        token = token.strip().lower()
        if not token:
            continue
        if _EMAIL_RE.match(token):
            if token not in seen:
                seen.append(token)
            if len(seen) >= limit:
                break
        else:
            invalid.append(token)
    return seen, invalid


def with_prefix(subject: str) -> str:
    """Prepend EMAIL_SUBJECT_PREFIX when set (customizable subjects)."""
    prefix = (config.EMAIL_SUBJECT_PREFIX or "").strip()
    subject = (subject or "").strip() or "Royal Stock report"
    if prefix and not subject.startswith(f"[{prefix}]"):
        return f"[{prefix}] {subject}"
    return subject


def _from_header() -> str:
    """'Friendly Name <addr>' From header (falls back to bare address)."""
    addr = config.SMTP_FROM or config.SMTP_USER
    _, bare = parseaddr(addr)
    bare = bare or addr
    name = (config.EMAIL_FROM_NAME or "").strip()
    if name and bare:
        return formataddr((name, bare))
    return bare


def text_to_html_lines(text: str) -> list[str]:
    """Turn plain custom text into safe HTML lines (blank line = paragraph gap)."""
    import html as _html

    lines: list[str] = []
    for para in str(text or "").splitlines() or [""]:
        para = para.strip()
        lines.append("<br>" if not para else _html.escape(para))
    return lines


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
    subject = with_prefix(subject)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _from_header()
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


def send_custom(to: str, subject: str, body_text: str) -> tuple[bool, str]:
    """Send a free-form customizable mail (plain text -> styled HTML).

    `to` may hold several comma/space separated addresses (max 5); every
    recipient gets the same mail. Subject is optional (a default is used)
    and EMAIL_SUBJECT_PREFIX is honoured. Returns (ok, error_message).
    """
    recipients, invalid = parse_recipients(to)
    if invalid:
        return False, f"invalid address: {invalid[0]}"
    if not recipients:
        return False, "invalid recipient address"
    body = (body_text or "").strip()
    if not body:
        return False, "message body is empty"
    if len(body) > 20000:
        return False, "message too long (max 20000 characters)"
    lines = text_to_html_lines(body)
    ok, error = send_email(", ".join(recipients), subject or "Royal Stock note", lines)
    return ok, error


def send_email(to: str, subject: str, html_lines: list[str]) -> tuple[bool, str]:
    """Send report lines to one or more mail ids. Returns (ok, error_message).

    `to` accepts a single address or several comma/space separated ones
    (max 5). Transport priority: Resend HTTPS API when RESEND_API_KEY is set
    (works on hosts where SMTP ports are blocked), with the SMTP chain as a
    fallback when Resend fails AND full SMTP credentials exist. Without a
    Resend key the SMTP chain runs directly; its errors name host:port.
    """
    recipients, invalid = parse_recipients(to)
    if invalid:
        return False, f"invalid recipient address: {invalid[0]}"
    if not recipients:
        return False, "invalid recipient address"
    if not is_configured():
        return False, (
            "email is not configured on the server "
            "(set RESEND_API_KEY, or SMTP_HOST/SMTP_USER/SMTP_PASS)"
        )
    from . import resend as resend_mod

    subject = with_prefix(subject)
    # One message per recipient so a typo in one address never blocks the rest.
    # Single-recipient keeps the legacy contract: (True, "") on SMTP success
    # and (True, "resend id: ...") on Resend success, so existing callers and
    # tests are unaffected; multi-recipient returns a combined summary.
    single = len(recipients) == 1
    sent: list[str] = []
    sent_info: list[str] = []
    failures: list[str] = []
    for recipient in recipients:
        if resend_mod.is_configured():
            ok, info = resend_mod.send_via_resend(
                recipient, subject,
                _html_document(subject, html_lines),
                _plain_fallback(html_lines),
            )
            if ok:
                # info carries "resend id: ..." on success (delivery tracking).
                if single:
                    return True, info
                sent.append(recipient)
                if info:
                    sent_info.append(f"{recipient} ({info})")
                continue
            smtp_usable = bool(
                config.SMTP_HOST.strip()
                and config.SMTP_USER.strip()
                and config.SMTP_PASS
            )
            if not smtp_usable:
                failures.append(f"{recipient}: {info}")
                continue
            log.warning(
                "resend failed (%s) - falling back to SMTP %s", info, config.SMTP_HOST
            )
            smtp_ok, smtp_error = _smtp_attempt(recipient, subject, html_lines)
            if smtp_ok:
                if single:
                    return True, ""
                sent.append(recipient)
            else:
                failures.append(f"{recipient}: {info}; smtp fallback also failed ({smtp_error})")
            continue
        smtp_ok, smtp_error = _smtp_attempt(recipient, subject, html_lines)
        if smtp_ok:
            if single:
                return True, ""
            sent.append(recipient)
        else:
            failures.append(f"{recipient}: {smtp_error}")
    if single:
        return False, "; ".join(failures)
    if sent and not failures:
        detail = f"sent to {', '.join(sent)}"
        if sent_info:
            detail += f" ({'; '.join(sent_info)})"
        return True, detail
    if sent:
        return True, f"sent to {', '.join(sent)}; failed: {'; '.join(failures)}"
    return False, "; ".join(failures)
