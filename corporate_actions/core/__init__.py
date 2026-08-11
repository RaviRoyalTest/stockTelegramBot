"""Core primitives shared across every layer of the app.

Nothing in this package imports application modules - these helpers are the
bottom of the dependency pyramid (dates, numbers, text) and can be imported
from anywhere without creating cycles.
"""
from .dates import (
    IST,
    format_date,
    format_timestamp,
    next_at_ist,
    parse_iso_date,
    parse_nse_date,
    today_ist,
)
from .numbers import format_money, format_num
from .text import escape, split_messages, strip_html

__all__ = [
    "IST",
    "format_date",
    "format_timestamp",
    "next_at_ist",
    "parse_iso_date",
    "parse_nse_date",
    "today_ist",
    "format_money",
    "format_num",
    "escape",
    "split_messages",
    "strip_html",
]
