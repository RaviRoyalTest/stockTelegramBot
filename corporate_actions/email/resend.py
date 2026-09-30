"""HTTPS email delivery via Resend (no SMTP ports needed).

For hosts where outbound SMTP (587/465) is unreachable, Resend's HTTPS API
(port 443, same port Telegram itself uses) still gets through. Free tier
covers daily bot digests comfortably. Setup (5 minutes, no app password):

  1. Sign up at https://resend.com (free, no card).
  2. Dashboard -> API Keys -> Create API Key -> copy the `re_...` key.
  3. Set RESEND_API_KEY on the bot host (Render env). Optional RESEND_FROM,
     defaults to "Royal Stock <onboarding@resend.dev>" which works without
     verifying a domain.

Uses only the standard library (urllib + json).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .. import config

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "Royal Stock <onboarding@resend.dev>"
# api.resend.com sits behind Cloudflare, which BLOCKS urllib's default
# "Python-urllib/3.x" User-Agent with HTTP 403 "error code: 1010" before the
# request ever reaches Resend (reproduced and verified: any explicit UA -
# custom, curl, python-requests - passes and gets a real API response).
# Sending an honest bot UA fixes it; keep it non-browser-like on purpose.
USER_AGENT = "RoyalStockBot/1.0 (+https://github.com/RaviRoyalTest/stockTelegramBot)"


def is_configured() -> bool:
    """True when a Resend API key is present."""
    return bool((config.RESEND_API_KEY or "").strip())


def send_via_resend(to: str, subject: str, html_body: str,
                    text_body: str) -> tuple[bool, str]:
    """POST one email through Resend. Returns (ok, error_message)."""
    recipient = (to or "").strip()
    payload = json.dumps({
        "from": config.RESEND_FROM or DEFAULT_FROM,
        "to": [recipient],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }).encode("utf-8")
    request = urllib.request.Request(
        RESEND_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY.strip()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        log.warning("resend to %s failed: HTTP %s %s", recipient, error.code, detail)
        return False, f"resend rejected the request (HTTP {error.code}: {detail or error})"
    except Exception as error:
        log.warning("resend to %s failed: %s", recipient, error)
        return False, f"resend unreachable ({config.redact(str(error))})"
    if status not in (200, 201, 202):
        return False, f"resend returned HTTP {status}: {body[:200]}"
    try:
        email_id = str(json.loads(body).get("id") or "")
    except Exception:
        email_id = ""
    log.info("email sent via resend to %s: %s (id %s)", recipient, subject, email_id or "?")
    # On success the second tuple item carries the Resend message id so
    # callers can show it - that id is what the resend.com dashboard lists
    # deliveries/bounces under.
    return True, (f"resend id: {email_id}" if email_id else "")
