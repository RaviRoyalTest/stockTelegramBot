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
from .daily import build_all_actions_lines, build_gaps_lines
from .daily import build_nifty_actions_lines, build_nifty_block, build_open_lines
from .daily import build_us_lines, build_watchlist_actions_lines, build_watchlist_block
from .daily import fetch_morning_gaps
from .daily import fetch_nifty_actions, fetch_watchlist_actions, get_scope
from .daily import maybe_send_daily_email, maybe_send_eod_email, maybe_send_open_email
from .daily import split_gaps
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
    "build_all_actions_lines",
    "build_close_lines",
    "build_combined_lines",
    "build_daily_lines",
    "build_eod_store_lines",
    "build_full_session_lines",
    "build_gaps_lines",
    "build_nifty_actions_lines",
    "build_nifty_block",
    "build_open_lines",
    "build_us_lines",
    "build_watchlist_actions_lines",
    "build_watchlist_block",
    "fetch_morning_gaps",
    "fetch_nifty_actions",
    "fetch_watchlist_actions",
    "get_scope",
    "maybe_send_daily_email",
    "maybe_send_eod_email",
    "maybe_send_open_email",
    "split_gaps",
    "resend_configured",
    "send_via_resend",
]
