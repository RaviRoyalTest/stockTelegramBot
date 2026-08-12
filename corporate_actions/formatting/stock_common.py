"""Shared stock-formatting helpers (no market-specific rendering).

Pure formatting primitives used by BOTH the Indian renderers (stock_india.py)
and the US renderer (stock_us.py): 52-week zone signal, RSI labels and the
small number/percent formatters. Market-specific renderers live in their own
modules so Indian and US output can never drift apart accidentally.
"""
from __future__ import annotations


def _wk52_signal(price, fund: dict | None) -> tuple:
    """Return (signal_emoji, range_tag) based on 52-week position of price."""
    if not fund:
        return "", ""
    low = fund.get("wk52_low")
    high = fund.get("wk52_high")
    if low is None or high is None or price is None:
        return "", ""
    try:
        price = float(price)
        low = float(low)
        high = float(high)
    except (TypeError, ValueError):
        return "", ""
    spread = high - low
    if spread <= 0:
        return "", ""
    percent_position = (price - low) / spread  # 0.0 = at 52W low, 1.0 = at 52W high
    if percent_position <= 0.15:
        return "\u2705", "\U0001F7E2 Near 52W Low"
    if percent_position <= 0.35:
        return "\U0001F4C8", "\U0001F7E2 Low Zone"
    if percent_position >= 0.85:
        return "\U0001F6AB", "\U0001F534 Near 52W High"
    if percent_position >= 0.65:
        return "\u26a0\ufe0f", "\U0001F534 High Zone"
    return "\U0001F7E1", "\U0001F7E1 Mid-Range"


def _rsi_signal(rsi: float | None) -> str:
    """Format 14-period RSI with clear signal emoji."""
    if rsi is None:
        return ""
    if rsi <= 30.0:
        return f"\U0001F7E2 RSI {rsi:g} (Oversold)"
    if rsi <= 45.0:
        return f"\U0001F7E2 RSI {rsi:g} (Low)"
    if rsi >= 70.0:
        return f"\U0001F534 RSI {rsi:g} (Overbought)"
    if rsi >= 60.0:
        return f"\U0001F534 RSI {rsi:g} (High)"
    return f"\U0001F7E1 RSI {rsi:g}"


def _num_or_na(value, decimals: int) -> str:
    try:
        formatted = f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _pct_str(value) -> str:
    """Fraction (0.18) -> signed percent string ('+18.0%'), 'N/A' when missing."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _growth_pct_str(value) -> str:
    """YoY growth % with a green/red arrow so up/down reads at a glance."""
    formatted = _pct_str(value)
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return formatted
    arrow = "\U0001F7E2\u25b2" if numeric_value >= 0 else "\U0001F534\u25bc"
    return f"{arrow} {formatted}"
