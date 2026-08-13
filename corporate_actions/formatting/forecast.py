"""Analyst forecast & company-watch report renderer for /forecast (pure).

Composes the "forecast value" view from the fundamentals dict: analyst
consensus + rating breakdown, the 12-month target price with upside, the top
executives (Yahoo companyOfficers) and - for NSE stocks - the top competitors
by market cap (screener.in peers). Used by /forecast; the same pieces also
appear inside the deep reports (/fundamentalreport, /usstock).
"""
from __future__ import annotations

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import (
    _executive_lines,
    _ratings_text,
    _rec_history_lines,
    _target_range_lines,
)


def build_forecast_lines(raw_symbol: str, quote: dict, fund: dict,
                         us: bool = False) -> list[str]:
    """Full /forecast report lines (Telegram HTML), IN (₹) or US ($).

    `quote` carries price/change/name; `fund` the fundamentals dict with
    rec_mean, rec_trend, target_*, officers and (Indian) competitors.
    """
    price = quote.get("price")
    currency = "USD" if us else "INR"
    lines = []
    company = quote.get("name") or raw_symbol
    lines.append(f"\U0001F4CA <b>{escape(str(company).upper())}</b> (<code>{escape(raw_symbol)}</code>)")
    if price is not None:
        move = ""
        change_pct = quote.get("change_pct")
        if change_pct is not None:
            arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
            color = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
            move = f"  {color}{arrow} {change_pct:+.2f}%"
        lines.append(f"Price: <b>{format_money(price, currency)}</b>{move}")
    lines.append("")

    # --- Analyst analysis & forecast ---
    has_analysis = any(fund.get(key) for key in
                       ("rec_mean", "rec_trend", "num_analysts", "target_mean",
                        "target_high", "target_low"))
    if has_analysis:
        lines.append("<b>\U0001F52D ANALYST ANALYSIS & FORECAST</b>")
        ratings = _ratings_text(fund)
        if ratings:
            lines.append(ratings)
        if fund.get("num_analysts"):
            lines.append(f"Analysts covering: <b>{fund['num_analysts']}</b>")
        lines.extend(_target_range_lines(fund, currency, price=price))
        lines.extend(_rec_history_lines(fund))
        if fund.get("analyst_source"):
            lines.append(f"Source: <i>{escape(str(fund['analyst_source']))}</i>")
        lines.append("")
    else:
        # Never silently show competitors-only: every forecast source was
        # unavailable, so say so instead of looking like the forecast is gone.
        lines.append("<b>\U0001F52D ANALYST ANALYSIS & FORECAST</b>")
        lines.append(
            "\U0001F4E1 <i>Analyst price forecast is temporarily unavailable from "
            "all sources right now (Yahoo rate-limited, independent sources "
            "unreachable). Try again in a few minutes, or use "
            "/fundamentalreport for the deep report.</i>"
        )
        lines.append("")

    # --- Top executives ---
    exec_lines = _executive_lines(fund, limit=6)
    if exec_lines:
        lines.extend(exec_lines)
        lines.append("")

    # --- Top competitors (NSE stocks only - screener.in peers) ---
    competitors = fund.get("competitors") or []
    if competitors:
        lines.append("<b>\U0001F3E2 TOP COMPETITORS</b>")
        lines.append(f"Peers by market cap \u2014 {len(competitors)} compared:")
        for peer in competitors[:6]:
            bits = []
            if peer.get("price") is not None:
                bits.append(f"CMP \u20b9{peer['price']:,.1f}")
            if peer.get("market_cap") is not None:
                bits.append(f"MCap \u20b9{peer['market_cap']:,.0f}Cr")
            if peer.get("pe") is not None:
                bits.append(f"P/E {peer['pe']:.1f}")
            if peer.get("roce") is not None:
                bits.append(f"ROCE {peer['roce']:.1f}%")
            lines.append(
                f"  \u2022 <b>{escape(peer['name'])}</b>"
                + (" \u2014 " + "  \u00b7  ".join(bits) if bits else "")
            )
        lines.append("")

    lines.append(f"\U0001F4A1 <i>Tip: /fundamentalreport {escape(raw_symbol)} for the full "
                 f"deep report \u00b7 /indicator {escape(raw_symbol)} for technicals.</i>")
    return lines
