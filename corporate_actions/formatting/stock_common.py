"""Shared stock-formatting helpers (market-neutral primitives).

Pure formatting primitives used by BOTH the Indian renderers (stock_india_*)
and the US renderer (stock_us.py): 52-week zone signal, RSI labels and the
small number/percent formatters, plus the INR _cr_str money helper used by the
Indian card/report. Market-specific renderers live in their own modules so
Indian and US output can never drift apart accidentally.
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
            macd_text += f"  \u2014  {'\U0001F7E2 Bullish crossover' if bull else '\U0001F534 Bearish crossover'}"
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
    """YoY growth % with a green/red arrow so up/down reads at a glance."""
    formatted = _pct_str(value)
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return formatted
    arrow = "\U0001F7E2\u25b2" if numeric_value >= 0 else "\U0001F534\u25bc"
    return f"{arrow} {formatted}"


def _cr_str(value) -> str:
    """INR money (raw rupees) -> '₹1,234.5Cr', or 'N/A' (used by Indian renderers)."""
    if value is None:
        return "N/A"
    try:
        return f"\u20b9{float(value) / 1e7:,.1f}Cr"
    except (TypeError, ValueError):
        return "N/A"
