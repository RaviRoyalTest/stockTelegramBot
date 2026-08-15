"""Shared stock-formatting helpers (market-neutral primitives).

Pure formatting primitives used by BOTH the Indian renderers (stock_india_*)
and the US renderer (stock_us.py): 52-week zone signal, RSI labels, the
small number/percent formatters, the INR _cr_str money helper, and the
analyst/executive helpers (_rec_label, _ratings_text, _executive_lines,
_top_officer). Market-specific renderers live in their own modules so Indian
and US output can never drift apart accidentally.
"""
from __future__ import annotations

import re

from ..core.numbers import format_money
from ..core.text import escape

_GREEN = "\U0001F7E2"
_YELLOW = "\U0001F7E1"
_RED = "\U0001F534"
_DOWN = "\U0001F53B"

_DIVIDER = "\u2501" * 20


def _section(emoji: str, title: str) -> list[str]:
    """A section header + divider line."""
    return [f"{emoji} <b>{title}</b>", _DIVIDER]


def _position_label(price, fund: dict) -> str | None:
    """52-week position label, e.g. '🟢 At High' / '🔴 At Low' / '🟡 Mid-Range'."""
    low = fund.get("wk52_low")
    high = fund.get("wk52_high")
    if low is None or high is None or price is None:
        return None
    try:
        price = float(price)
        low = float(low)
        high = float(high)
    except (TypeError, ValueError):
        return None
    spread = high - low
    if spread <= 0:
        return None
    percent_position = (price - low) / spread
    if percent_position >= 0.95:
        return f"{_GREEN} At High"
    if percent_position >= 0.75:
        return f"{_GREEN} Near High"
    if percent_position <= 0.05:
        return f"{_RED} At Low"
    if percent_position <= 0.25:
        return f"{_RED} Near Low"
    return f"{_YELLOW} Mid-Range"


def _inr_group(value, decimals: int = 0) -> str:
    """Format with Indian digit grouping (1,05,647) and fixed decimals."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "-" if number < 0 else ""
    number = abs(number)
    text = f"{number:.{decimals}f}"
    if "." in text:
        int_part, frac_part = text.split(".")
    else:
        int_part, frac_part = text, ""
    if len(int_part) <= 3:
        grouped = int_part
    else:
        head, tail = int_part[:-3], int_part[-3:]
        parts = [tail]
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts)
    if frac_part:
        return f"{sign}{grouped}.{frac_part}"
    return f"{sign}{grouped}"


def _num_1dp(value) -> str:
    """Number with exactly one decimal ('8.0'), or 'N/A'."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "N/A"


def _holding_str(value) -> str:
    """'38.09% (🟢 +0.48%)' -> '38.09% 🟢 +0.48%'."""
    match = re.match(r"([\d.]+%)\s*\(([^)]+)\)", value or "")
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value or ""


def _holding_delta(value):
    """Signed QoQ delta from a shareholding string ('(🟢 +0.48%)' -> 0.48)."""
    match = re.search(r"([+-]?\d+(?:\.\d+)?)%\)", value or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _holding_trend(value):
    """'🟢 Positive' / '🔴 Negative' / None from a shareholding delta."""
    delta = _holding_delta(value)
    if delta is None:
        return None
    return (_GREEN, "Positive") if delta > 0 else (_RED, "Negative")


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


def _macd_tag(fund: dict) -> str:
    """Compact MACD status for quick cards / movers rows, e.g. '\U0001F7E2 MACD 1.24 (Bullish)'."""
    line, signal = fund.get("macd_line"), fund.get("macd_signal")
    if line is None or signal is None:
        return ""
    bull = line >= signal
    icon = "\U0001F7E2" if bull else "\U0001F534"
    return f"{icon} MACD {_num_or_na(line, 2)} ({'Bullish' if bull else 'Bearish'})"


def _tech_indicator_lines(fund: dict, price=None) -> list[str]:
    """\U0001F4C8 RSI / MACD / SMA detail lines shared by the IN + US deep reports.

    Renders the full technical section: RSI(14) with its zone signal, the
    MACD(12,26,9) line/signal/histogram plus crossover direction, and the
    50d/200d simple moving averages with price position vs each.
    """
    rsi_tag = _rsi_signal(fund.get("rsi"))
    line, signal, hist = fund.get("macd_line"), fund.get("macd_signal"), fund.get("macd_hist")
    macd_bits = []
    if line is not None:
        macd_bits.append(f"MACD <b>{_num_or_na(line, 2)}</b>")
    if signal is not None:
        macd_bits.append(f"Signal <b>{_num_or_na(signal, 2)}</b>")
    if hist is not None:
        macd_bits.append(f"Hist <b>{_num_or_na(hist, 2)}</b>")
    out = []
    if not (rsi_tag or macd_bits):
        return out
    out.append("<b>\U0001F4C8 TECHNICAL INDICATORS</b>")
    if rsi_tag:
        out.append(f"\u26a1 RSI(14): {rsi_tag}")
    if macd_bits:
        macd_text = "  \u00b7  ".join(macd_bits)
        if line is not None and signal is not None:
            bull = line >= signal
            macd_text += "  \u2014  " + ("\U0001F7E2 Bullish crossover" if bull else "\U0001F534 Bearish crossover")
        out.append("\U0001F52C MACD(12,26,9): " + macd_text)
    sma_50, sma_200 = fund.get("sma_50"), fund.get("sma_200")
    if sma_50 is not None or sma_200 is not None:
        sma_parts = []
        if sma_50 is not None:
            sma_parts.append(f"50d {_num_or_na(sma_50, 1)}")
        if sma_200 is not None:
            sma_parts.append(f"200d {_num_or_na(sma_200, 1)}")
        sma_text = "\U0001F4C9 SMA: " + "  \u00b7  ".join(sma_parts)
        if price is not None:
            try:
                price_float = float(price)
                marks = []
                if sma_50 is not None:
                    marks.append(("50d", price_float >= float(sma_50)))
                if sma_200 is not None:
                    marks.append(("200d", price_float >= float(sma_200)))
                if marks:
                    above = sum(1 for _, is_above in marks if is_above)
                    icon = "\U0001F7E2" if above == len(marks) else ("\U0001F534" if above == 0 else "\U0001F7E1")
                    positions = " & ".join(
                        f"{name} {'above' if is_above else 'below'}" for name, is_above in marks
                    )
                    sma_text += f"  \u2014  {icon} Price {positions}"
            except (TypeError, ValueError):
                pass
        out.append(sma_text)
    return out


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
    """YoY growth % with a green/red indicator so up/down reads at a glance."""
    formatted = _pct_str(value)
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return formatted
    icon = "\U0001F7E2" if numeric_value >= 0 else "\U0001F53B"
    return f"{icon} {formatted}"


def _cr_str(value) -> str:
    """INR money (raw rupees) -> '₹1,234.5Cr', or 'N/A' (used by Indian renderers)."""
    if value is None:
        return "N/A"
    try:
        return f"\u20b9{float(value) / 1e7:,.1f}Cr"
    except (TypeError, ValueError):
        return "N/A"


def _rec_label(mean) -> str | None:
    """Yahoo recommendation mean (1=Strong Buy ... 5=Strong Sell) -> a label."""
    if mean is None:
        return None
    try:
        value = float(mean)
    except (TypeError, ValueError):
        return None
    if value <= 1.5:
        return "Strong Buy"
    if value <= 2.5:
        return "Buy"
    if value <= 3.5:
        return "Hold"
    if value <= 4.5:
        return "Sell"
    return "Strong Sell"


_RATING_LABELS = (
    ("strong_buy", "Strong Buy"), ("buy", "Buy"), ("hold", "Hold"),
    ("sell", "Sell"), ("strong_sell", "Strong Sell"),
)


def _consensus_label(fund: dict) -> str | None:
    """Consensus label: Yahoo's own recommendationKey wins, else derive from the mean."""
    rec_key = (fund.get("rec_key") or "").strip().replace("_", " ").title()
    if rec_key in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"):
        return rec_key
    return _rec_label(fund.get("rec_mean"))


def _ratings_text(fund: dict) -> str:
    """Analyst consensus + full rating breakdown (ALL five buckets, even 0)."""
    parts = []
    if fund.get("rec_mean") is not None:
        label = _consensus_label(fund)
        parts.append(f"Consensus: <b>{label}</b> ({fund['rec_mean']:.2f}/5)")
    trend = fund.get("rec_trend") or {}
    counts = [f"{label} {trend.get(key, 0)}" for key, label in _RATING_LABELS]
    if any(trend.values()):
        parts.append("  \u00b7  ".join(counts))
    return "  \u00b7  ".join(parts)


def _rec_history_lines(fund: dict) -> list[str]:
    """Rating-breakdown history across the last 4 periods (now, 1m, 2m, 3m ago).

    Shows EVERY period Yahoo reports so no analyst rating is left out, with a
    \u25b2/\u25bc arrow vs the previous period for the at-a-glance trend.
    """
    history = fund.get("rec_history") or []
    if len(history) < 2:
        return []
    lines = ["\U0001F4C5 <b>Rating trend (last 4 periods)</b>"]
    labels = dict(_RATING_LABELS)
    for index, row in enumerate(history):
        period = (row.get("period") or "").strip()
        if period in ("", "0m", "0"):
            when = "Now"
        else:
            when = f"{period.lstrip('-').rstrip('m')}m ago"
        buckets = "  \u00b7  ".join(
            f"{labels[key]} {row.get(key, 0)}" for key, _ in _RATING_LABELS
        )
        arrow = ""
        if index > 0:
            previous = history[index - 1]
            diff = sum(row.get(key, 0) for key, _ in _RATING_LABELS) - \
                   sum(previous.get(key, 0) for key, _ in _RATING_LABELS)
            if diff > 0:
                arrow = "  \U0001F7E2"
            elif diff < 0:
                arrow = "  \U0001F53B"
        lines.append(f"  \u2022 {when}: {buckets}{arrow}")
    return lines


def _target_range_lines(fund: dict, currency: str = "INR", price=None) -> list[str]:
    """12-month target lines: mean with upside, plus high / median / low.

    `price` may come from the live quote (the fundamentals dict does not
    always carry it) - used only for the upside/downside percent.
    """
    lines = []
    if fund.get("target_mean") is not None:
        target_mean = float(fund["target_mean"])
        if price is None:
            price = fund.get("price")
        upside = ""
        if price:
            percent = (target_mean - float(price)) / float(price) * 100.0
            if percent > 0:
                upside = f"  (<b>+{percent:.0f}%</b> upside)"
            elif percent < 0:
                upside = f"  (<b>{percent:.0f}%</b> downside)"
        lines.append(
            f"Forecast target (mean): <b>{format_money(target_mean, currency)}</b>{upside}"
        )
    target_range = []
    if fund.get("target_high") is not None:
        target_range.append(f"High {format_money(fund['target_high'], currency)}")
    if fund.get("target_median") is not None:
        target_range.append(f"Median {format_money(fund['target_median'], currency)}")
    if fund.get("target_low") is not None:
        target_range.append(f"Low {format_money(fund['target_low'], currency)}")
    if target_range:
        lines.append("  " + "  \u00b7  ".join(target_range))
    return lines


def _executive_lines(fund: dict, limit: int = 5) -> list[str]:
    """Top-executives section lines (Yahoo companyOfficers), or [] when none."""
    officers = fund.get("officers") or []
    if not officers:
        return []
    lines = ["<b>\U0001F464 TOP EXECUTIVES</b>"]
    for officer in officers[:limit]:
        name = (officer.get("name") or "").strip()
        title = (officer.get("title") or "").strip()
        if name:
            lines.append(f"  \u2022 <b>{escape(name)}</b> \u2014 {escape(title or 'Director')}")
    return lines


def _top_officer(fund: dict):
    """(name, title) of the most senior executive for the quick card, or None."""
    officers = fund.get("officers") or []
    if not officers:
        return None
    name = (officers[0].get("name") or "").strip()
    if not name:
        return None
    return name, (officers[0].get("title") or "Director").strip()
