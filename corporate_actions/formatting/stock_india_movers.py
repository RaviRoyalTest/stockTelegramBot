"""Indian movers-row fundamentals lines (used by /topmovers & co).

A single compact block of signals + valuation + 52W/returns + shareholding
appended under each stock row in the movement screens. Purely a formatter
over the quote/fund dicts - fetching happens in the movers command module.
"""
from __future__ import annotations

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import _rsi_signal, _wk52_signal


def _fundamentals_lines(fund: dict | None, price=None) -> list[str]:
    """Format fundamentals as clean, spacious, structured lines."""
    if not fund:
        return []

    def _num(value, decimals: int) -> str:
        formatted = f"{value:.{decimals}f}"
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted

    sig_emoji, range_tag = _wk52_signal(price, fund)
    rsi_tag = _rsi_signal(fund.get("rsi"))

    lines = []

    # Line 1: Signals & Technicals
    l1_parts = []
    if range_tag:
        l1_parts.append(range_tag)
    if rsi_tag:
        l1_parts.append(rsi_tag)
    if l1_parts:
        lines.append("  \u2022  ".join(l1_parts))

    # Line 2: Valuation & Market Stats
    l2_parts = []
    if fund.get("pe"):
        l2_parts.append(f"P/E {_num(fund['pe'], 1)}")
    else:
        l2_parts.append("P/E N/A (Loss)")
    if fund.get("sector_pe"):
        l2_parts.append(f"Sec P/E {_num(fund['sector_pe'], 1)}")
    if fund.get("market_cap") is not None:
        l2_parts.append(f"MCap \u20b9{fund['market_cap']:,.0f}Cr")
    if fund.get("debt_to_equity") is not None:
        l2_parts.append(f"D/E {_num(fund['debt_to_equity'], 2)}")
    if l2_parts:
        lines.append("\U0001F4CA " + "  \u00b7  ".join(l2_parts))

    # Line 3: 52-Week Range & Returns
    l3_parts = []
    if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
        l3_parts.append(
            f"52w Range: {format_money(fund['wk52_low'])} \u2013 "
            f"{format_money(fund['wk52_high'])}"
        )
    if fund.get("div_yield") is not None:
        l3_parts.append(f"Div Yield: {_num(fund['div_yield'], 2)}%")
    if fund.get("roce") is not None or fund.get("roe") is not None:
        return_bits = []
        if fund.get("roce"):
            return_bits.append(f"ROCE {_num(fund['roce'], 1)}%")
        if fund.get("roe"):
            return_bits.append(f"ROE {_num(fund['roe'], 1)}%")
        l3_parts.append(" ".join(return_bits))
    if l3_parts:
        lines.append("\U0001F4C8 " + "  \u00b7  ".join(l3_parts))

    # Line 4: Shareholding Pattern (with QoQ trends!)
    if any(fund.get(key) for key in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        holding_bits = []
        for key, label in (
            ("promoter_pct", "Prom"),
            ("fii_pct", "FII"),
            ("dii_pct", "DII"),
            ("public_pct", "Pub"),
        ):
            if fund.get(key):
                holding_bits.append(f"{label} {escape(fund[key])}")
        lines.append("\U0001F4BC Holding (QoQ): " + "  \u00b7  ".join(holding_bits))

    return lines
