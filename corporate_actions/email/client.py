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
    """Wrap report lines in a colorful, readable, mobile-friendly document.

    Body lines may contain full <table class="rs-table"> blocks (built by
    corporate_actions.email.tables) as well as plain Telegram-style HTML.
    Tables render with striped rows, green/red move badges and a dark
    gradient header; everything is inline-CSS so Gmail/Outlook keep it.
    """
    import html as _html

    safe_title = _html.escape(str(title or "Royal Stock report"))
    body = "<br>\n".join(body_lines)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body style=\"margin:0;padding:0;background:#0f172a;"
        "font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0f172a;\">"
        "<div style=\"max-width:720px;margin:0 auto;padding:16px;\">"
        f"<div style=\"background:linear-gradient(135deg,#1d4ed8,#7c3aed);"
        "color:#ffffff;border-radius:14px;padding:18px 20px;margin-bottom:14px;\">"
        f"<div style=\"font-size:20px;font-weight:800;\">📈 {safe_title}</div>"
        "<div style=\"font-size:12px;opacity:0.85;margin-top:4px;\">"
        "Royal Stock · NSE &amp; BSE · session screener + store summary</div></div>"
        f"<div style=\"background:#ffffff;border-radius:14px;padding:16px 16px;"
        f"border:1px solid #e2e8f0;\">{body}</div>"
        "<style>"
        ".rs-sec{margin:14px 0 6px;padding:10px 12px;border-radius:10px;font-weight:800;"
        "font-size:15px;color:#ffffff;background:linear-gradient(135deg,#0ea5e9,#6366f1);}"
        ".rs-sec.green{background:linear-gradient(135deg,#059669,#10b981);}"
        ".rs-sec.red{background:linear-gradient(135deg,#dc2626,#f59e0b);}"
        ".rs-sec.amber{background:linear-gradient(135deg,#d97706,#f59e0b);}"
        ".rs-sec.slate{background:linear-gradient(135deg,#334155,#64748b);}"
        ".rs-table{width:100%;border-collapse:collapse;margin:8px 0 12px;font-size:13px;}"
        ".rs-table th{background:#1e293b;color:#fff;padding:8px 6px;text-align:left;"
        "font-size:12px;white-space:nowrap;}"
        ".rs-table th.num,.rs-table td.num{text-align:right;white-space:nowrap;}"
        ".rs-table td{padding:7px 6px;border-bottom:1px solid #e2e8f0;}"
        ".rs-table tr:nth-child(even) td{background:#f8fafc;}"
        ".pos{color:#059669;font-weight:700;}"
        ".neg{color:#dc2626;font-weight:700;}"
        ".pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;"
        "font-weight:700;background:#eef2ff;color:#3730a3;}"
        ".pill.pos{background:#dcfce7;color:#166534;}"
        ".pill.neg{background:#fee2e2;color:#991b1b;}"
        ".muted{color:#64748b;font-size:12px;}"
        "</style>"
        "<p class=\"muted\" style=\"color:#94a3b8;font-size:12px;text-align:center;\">"
        "Sent by Royal Stock bot · manage with /dailyemail off</p>"
        "</div></body></html>"
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


def _log_send(chat_id, kind: str, to: str, subject: str,
              ok: bool, info: str) -> None:
    """Append one entry to the mail send log (best-effort, never raises)."""
    try:
        from .. import storage

        storage.record_mail(chat_id, kind, to, with_prefix(subject), ok, info)
    except Exception as error:
        log.debug("_log_send: %s", error)


def send_custom(to: str, subject: str, body_text: str,
               *, kind: str = "custom", chat_id=None) -> tuple[bool, str]:
    """Send a free-form customizable mail (plain text -> styled HTML).

    `to` may hold several comma/space separated addresses (max 5); every
    recipient gets the same mail. Subject is optional (a default is used)
    and EMAIL_SUBJECT_PREFIX is honoured. Returns (ok, error_message).
    The send is recorded in the mail log under `kind` for /emailstatus.
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
    # The inner send_email call does the mail-log recording (no double log).
    return send_email(", ".join(recipients), subject or "Royal Stock note",
                     lines, kind=kind, chat_id=chat_id)


def send_email(to: str, subject: str, html_lines: list[str],
               *, kind: str = "manual", chat_id=None) -> tuple[bool, str]:
    """Send report lines to one or more mail ids. Returns (ok, error_message).

    `to` accepts a single address or several comma/space separated ones
    (max 5). Transport priority: Resend HTTPS API when RESEND_API_KEY is set
    (works on hosts where SMTP ports are blocked), with the SMTP chain as a
    fallback when Resend fails AND full SMTP credentials exist. Without a
    Resend key the SMTP chain runs directly; its errors name host:port.
    Every send is recorded in the mail log (see /emailstatus).
    """
    recipients, invalid = parse_recipients(to)
    if invalid:
        return False, f"invalid recipient address: {invalid[0]}"
    if not recipients:
        return False, "invalid recipient address"
    if not is_configured():
        error = (
            "email is not configured on the server "
            "(set RESEND_API_KEY, or SMTP_HOST/SMTP_USER/SMTP_PASS)"
        )
        _log_send(chat_id, kind, to, subject, False, error)
        return False, error
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
    def _finish(ok: bool, info: str) -> tuple[bool, str]:
        _log_send(chat_id, kind, ", ".join(recipients), subject, ok, info)
        return ok, info

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
                    return _finish(True, info)
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
                    return _finish(True, "")
                sent.append(recipient)
            else:
                failures.append(f"{recipient}: {info}; smtp fallback also failed ({smtp_error})")
            continue
        smtp_ok, smtp_error = _smtp_attempt(recipient, subject, html_lines)
        if smtp_ok:
            if single:
                return _finish(True, "")
            sent.append(recipient)
        else:
            failures.append(f"{recipient}: {smtp_error}")
    if single:
        return _finish(False, "; ".join(failures))
    if sent and not failures:
        detail = f"sent to {', '.join(sent)}"
        if sent_info:
            detail += f" ({'; '.join(sent_info)})"
        return _finish(True, detail)
    if sent:
        return _finish(True, f"sent to {', '.join(sent)}; failed: {'; '.join(failures)}")
    return _finish(False, "; ".join(failures))
