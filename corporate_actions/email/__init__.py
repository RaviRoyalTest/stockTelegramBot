"""Email delivery package (SMTP, stdlib only).

Sends the bot's HTML reports to a user's mail id via any SMTP provider
(Gmail with an App Password, Outlook, etc.). No new dependencies:
smtplib + email.message from the standard library.
"""
from .client import EmailError, is_configured, send_custom, send_email
from .client import GMAIL_SETUP_GUIDE, parse_recipients, provider_name, status
from .client import text_to_html_lines, with_prefix
from .tables import esc as tables_esc
from .tables import muted, stat_chips
from .daily import build_close_lines, build_combined_lines, build_daily_lines
from .daily import build_eod_store_lines, build_full_session_lines
from .daily import build_open_lines, get_scope
from .daily import maybe_send_daily_email, maybe_send_eod_email, maybe_send_open_email
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
    "stat_chips",
    "status",
    "tables_esc",
    "text_to_html_lines",
    "with_prefix",
    "muted",
    "build_close_lines",
    "build_combined_lines",
    "build_daily_lines",
    "build_eod_store_lines",
    "build_full_session_lines",
    "build_open_lines",
    "get_scope",
    "maybe_send_daily_email",
    "maybe_send_eod_email",
    "maybe_send_open_email",
    "resend_configured",
    "send_via_resend",
]
