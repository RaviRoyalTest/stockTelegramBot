"""Indian quick analysis card renderer (/fundamentalanalyze, /stock).

A compact single-symbol card: price & movement, valuation & ratios, balance
sheet & earnings, profitability, analyst forecast, top executive and
shareholding. Uses the shared number / signal helpers from stock_common.py;
the DEEP report lives in stock_india_report.py and the movers rows in
stock_india_movers.py.
"""
from __future__ import annotations

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import (
    _GREEN,
    _RED,
    _YELLOW,
    _consensus_label,
    _holding_str,
    _inr_group,
    _num_1dp,
    _num_or_na,
    _pct_str,
    _position_label,
    _section,
    _top_officer,
)


def _price_move_line(quote: dict) -> str | None:
    """The 'Current Price: ...' line with green/red direction arrow (INR)."""
    price = quote.get("price")
    if price is None:
        return None
    change_pct = quote.get("change_pct")
    price_str = format_money(price)
    if change_pct is not None:
        sign = "+" if change_pct >= 0 else ""
        arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
        color_icon = _GREEN if change_pct >= 0 else _RED
        return (
            f"Current Price: <b>{price_str}</b> {color_icon}{arrow} "
            f"<b>{sign}{change_pct:.2f}%</b>"
        )
    return f"Current Price: <b>{price_str}</b>"


def _holding_lines(fund: dict, label_map: tuple) -> list[str]:
    out = []
    for key, emoji, label in label_map:
        if fund.get(key):
            out.append(f"{emoji} {label}: {escape(_holding_str(fund[key]))}")
    return out


def _rsi_short(rsi):
    """RSI with icon + zone, e.g. '🟢 <b>43.6</b> (Low)'."""
    if rsi is None:
        return None
    try:
        value = float(rsi)
    except (TypeError, ValueError):
        return None
    if value <= 30:
        icon, zone = _GREEN, "Oversold"
    elif value < 45:
        icon, zone = _GREEN, "Low"
    elif value >= 70:
        icon, zone = _RED, "Overbought"
    elif value >= 60:
        icon, zone = _RED, "High"
    else:
        icon, zone = _YELLOW, "Mid"
    return f"{icon} <b>{_num_or_na(value, 1)}</b> ({zone})"


def _macd_short(line, signal):
    """MACD with icon + trend, e.g. '🔴 <b>-123.85</b> (Bearish)'."""
    if line is None:
        return None
    bull = line >= signal
    icon = _GREEN if bull else _RED
    return f"{icon} <b>{_num_or_na(line, 2)}</b> ({'Bullish' if bull else 'Bearish'})"


def _stock_summary_lines(raw_symbol, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the compact /fundamentalanalyze card for one symbol."""
    quote = quote or {}
    fund = fund or {}
    price = quote.get("price")
    company_name = quote.get("name") or raw_symbol
    label_prefix = f"{label} " if label else ""

    lines = []
    lines.append(f"\U0001F4CA {label_prefix}<b>{escape(company_name.upper())}</b>")
    lines.append(f"(<code>{escape(raw_symbol)}</code>)")
    lines.append(f"Sector: <b>{escape(fund.get('sector') or 'Indian Equity')}</b>")
    lines.append("")

    # PRICE & MOVEMENT
    position = _position_label(price, fund)
    if (
        price is not None
        or (fund.get("wk52_high") is not None and fund.get("wk52_low") is not None)
        or position
        or fund.get("rsi") is not None
        or fund.get("macd_line") is not None
    ):
        lines.extend(_section("\U0001F4B0", "PRICE & MOVEMENT"))
        price_line = _price_move_line(quote)
        if price_line:
            lines.append(price_line)
        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            low, high = fund["wk52_low"], fund["wk52_high"]
            lines.append(f"52W Range: <b>\u20b9{_inr_group(low)} \u2013 \u20b9{_inr_group(high)}</b>")
            if price:
                try:
                    price_float = float(price)
                    low_dist = (price_float - float(low)) / float(low) * 100
                    high_dist = (price_float - float(high)) / float(high) * 100
                    lines.append(f"From 52W Low: <b>{low_dist:+.1f}%</b>")
                    lines.append(f"From 52W High: <b>{high_dist:+.1f}%</b>")
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        if position:
            lines.append(f"Technicals: <b>{position}</b>")
        rsi_tag = _rsi_short(fund.get("rsi"))
        if rsi_tag:
            lines.append(f"RSI(14): {rsi_tag}")
        macd_tag = _macd_short(fund.get("macd_line"), fund.get("macd_signal"))
        if macd_tag:
            lines.append(f"MACD: {macd_tag}")
        lines.append("")

    # VALUATION & RATIOS
    lines.extend(_section("\U0001F3F7\ufe0f", "VALUATION & RATIOS"))
    if fund.get("pe"):
        lines.append(f"Stock P/E: <b>{_num_or_na(fund['pe'], 1)}x</b>")
    else:
        lines.append("Stock P/E: <b>N/A (Loss)</b>")
    if fund.get("sector_pe"):
        lines.append(f"Sector P/E: <b>{_num_or_na(fund['sector_pe'], 1)}x</b>")
    mcap = fund.get("market_cap")
    if mcap is None:
        mcap = fund.get("mcap_cr")
    if mcap is not None:
        lines.append(f"Market Cap: <b>\u20b9{_inr_group(mcap)} Cr</b>")
    if fund.get("debt_to_equity") is not None:
        lines.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}x</b>")
    if fund.get("div_yield") is not None:
        lines.append(f"Dividend Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")
    lines.append("")

    # BALANCE SHEET & EARNINGS
    bs_parts = []
    if fund.get("debt_to_equity") is not None:
        bs_parts.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}x</b>")
    if fund.get("book_value") is not None:
        bs_parts.append(f"Book Value: <b>\u20b9{_inr_group(fund['book_value'], 2)}</b>")
    if fund.get("trailing_eps") is not None:
        bs_parts.append(f"EPS (TTM): <b>\u20b9{_num_or_na(fund['trailing_eps'], 0)}</b>")
    if fund.get("profit_margin") is not None:
        bs_parts.append(f"Net Margin: <b>{_pct_str(fund['profit_margin'])}</b>")
    if bs_parts:
        lines.extend(_section("\U0001F4C9", "BALANCE SHEET & EARNINGS"))
        lines.extend(bs_parts)
        lines.append("")

    # PROFITABILITY & RETURNS
    returns_parts = []
    if fund.get("roce") is not None:
        returns_parts.append(f"ROCE: <b>{_num_1dp(fund['roce'])}%</b>")
    if fund.get("roe") is not None:
        returns_parts.append(f"ROE: <b>{_num_1dp(fund['roe'])}%</b>")
    if returns_parts:
        lines.extend(_section("\U0001F3AF", "PROFITABILITY & RETURNS"))
        lines.extend(returns_parts)
        lines.append("")

    # ANALYST FORECAST
    forecast = []
    consensus = _consensus_label(fund)
    if consensus:
        icon = {"Strong Buy": _GREEN, "Buy": _GREEN, "Hold": _YELLOW,
                "Sell": _RED, "Strong Sell": _RED}.get(consensus, _YELLOW)
        mean = f" ({fund['rec_mean']:.1f}/5)" if fund.get("rec_mean") is not None else ""
        forecast.append(f"Consensus: {icon} <b>{consensus}</b>{mean}")
    if fund.get("target_mean") is not None:
        target = float(fund["target_mean"])
        forecast.append(f"Target Price: <b>\u20b9{_inr_group(target)}</b>")
        if price:
            try:
                upside = (target - float(price)) / float(price) * 100.0
                forecast.append(f"Upside: <b>{upside:+.0f}%</b>")
            except (TypeError, ValueError):
                pass
    if forecast:
        lines.extend(_section("\U0001F52D", "ANALYST FORECAST"))
        lines.extend(forecast)
        lines.append("")

    # TOP EXECUTIVE
    officer = _top_officer(fund)
    if officer:
        lines.extend(_section("\U0001F464", "TOP EXECUTIVE"))
        lines.append(f"<b>{escape(officer[0])}</b>")
        lines.append(escape(officer[1]))
        lines.append("")

    # SHAREHOLDING · QoQ
    if any(fund.get(key) for key in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        lines.extend(_section("\U0001F4BC", "SHAREHOLDING \u00b7 QoQ"))
        lines.extend(_holding_lines(
            fund,
            (("promoter_pct", "\U0001F451", "Promoter"),
             ("fii_pct", "\U0001F30D", "FII"),
             ("dii_pct", "\U0001F3DB\ufe0f", "DII"),
             ("public_pct", "\U0001F465", "Public")),
        ))
    else:
        lines.extend(_section("\U0001F4BC", "SHAREHOLDING \u00b7 QoQ"))
        lines.append("No shareholding breakdown available.")

    if include_tip:
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_symbol} NSE</i>")
    return lines
