"""Indian quick analysis card renderer (/fundamentalanalyze, /stock).

A compact single-symbol card: price & movement, valuation & ratios, balance
sheet & earnings, profitability and shareholding. Uses the shared number /
signal helpers from stock_common.py; the DEEP report lives in
stock_india_report.py and the movers rows in stock_india_movers.py.
"""
from __future__ import annotations

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import (
    _cr_str,
    _macd_tag,
    _num_or_na,
    _pct_str,
    _consensus_label,
    _rsi_signal,
    _top_officer,
    _wk52_signal,
)


def _price_move_line(quote: dict) -> str | None:
    """The 'Current Price: ...' line with green/red direction arrow (INR)."""
    price = quote.get("price")
    if price is None:
        return None
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    price_str = format_money(price)
    if change_pct is not None:
        sign = "+" if change_pct >= 0 else ""
        absolute_change_str = f" ({sign}{format_money(change_abs)})" if change_abs is not None else ""
        arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
        color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
        return (
            f"Current Price: <b>{price_str}</b>  {color_icon}{arrow} "
            f"<b>{sign}{change_pct:.2f}%</b>{absolute_change_str}"
        )
    return f"Current Price: <b>{price_str}</b>"


def _holding_lines(fund: dict, label_map: tuple) -> list[str]:
    out = []
    for key, emoji, label in label_map:
        if fund.get(key):
            out.append(f"{emoji} {label}: {escape(fund[key])}")
    return out


def _stock_summary_lines(raw_symbol, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the compact /stock summary card for one symbol."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    company_name = quote.get("name") or raw_symbol

    lines = []
    sector_name = escape(fund.get("sector") or "Indian Equity")
    label_prefix = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {label_prefix}<b>{escape(company_name.upper())}</b> (<code>{escape(raw_symbol)}</code>)"
    )
    lines.append(f"Sector: <i>{sector_name}</i>")
    lines.append("")

    # Section 1: Price & Today's Movement
    sig_emoji, range_tag = _wk52_signal(price, fund)
    rsi_tag = _rsi_signal(fund.get("rsi"))
    macd_tag = _macd_tag(fund)
    technical_bits = [signal for signal in (range_tag, rsi_tag, macd_tag) if signal]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or technical_bits:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        price_line = _price_move_line(quote)
        if price_line:
            lines.append(price_line)

        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            high, low = fund["wk52_high"], fund["wk52_low"]
            lines.append(f"\U0001F4C8 52w Range: {format_money(low)} \u2013 {format_money(high)}")
            if price:
                try:
                    distance_from_low = ((float(price) - low) / low) * 100
                    distance_from_high = ((high - float(price)) / high) * 100
                    lines.append(f"📍 Distance: +{distance_from_low:.1f}% from 52w Low  \u00b7  -{distance_from_high:.1f}% from 52w High")
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

        if technical_bits:
            lines.append(f"\u26a1 Technicals: {'  •  '.join(technical_bits)}")
        lines.append("")

    # Section 2: Valuation & Ratios
    lines.append("<b>\U0001F3F7\ufe0f VALUATION & RATIOS</b>")
    valuation_parts = []
    if fund.get("pe"):
        valuation_parts.append(f"Stock P/E: <b>{_num_or_na(fund['pe'], 1)}</b>")
    else:
        valuation_parts.append("Stock P/E: <b>N/A (Loss)</b>")

    if fund.get("sector_pe"):
        valuation_parts.append(f"Sec P/E: <b>{_num_or_na(fund['sector_pe'], 1)}</b>")

    if fund.get("market_cap") is not None:
        valuation_parts.append(f"MCap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")

    if fund.get("debt_to_equity") is not None:
        valuation_parts.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}</b>")

    if fund.get("div_yield") is not None:
        valuation_parts.append(f"Div Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")

    lines.append("  \u00b7  ".join(valuation_parts))
    lines.append("")

    # Section 3: Balance sheet & earnings (D/E, liquidity, per-share, cash)
    bs_parts = []
    if fund.get("debt_to_equity") is not None:
        bs_parts.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}</b>")
    if fund.get("current_ratio") is not None:
        bs_parts.append(f"Current Ratio: <b>{_num_or_na(fund['current_ratio'], 2)}</b>")
    if fund.get("book_value") is not None:
        bs_parts.append(f"Book Value: <b>{_num_or_na(fund['book_value'], 2)}</b>")
    if fund.get("trailing_eps") is not None:
        bs_parts.append(f"EPS(TTM): <b>{_num_or_na(fund['trailing_eps'], 2)}</b>")
    if fund.get("profit_margin") is not None:
        bs_parts.append(f"Net Margin: <b>{_pct_str(fund['profit_margin'])}</b>")
    if bs_parts or fund.get("total_cash") is not None or fund.get("total_debt") is not None:
        lines.append("<b>\U0001F4C9 BALANCE SHEET & EARNINGS</b>")
        if bs_parts:
            lines.append("  \u00b7  ".join(bs_parts))
        if fund.get("total_cash") is not None or fund.get("total_debt") is not None:
            lines.append(
                f"Cash: <b>{_cr_str(fund.get('total_cash'))}</b>  \u00b7  "
                f"Debt: <b>{_cr_str(fund.get('total_debt'))}</b>"
            )
        lines.append("")

    # Section 4: Profitability & Returns
    lines.append("<b>\U0001F3AF PROFITABILITY & RETURNS</b>")
    returns_parts = []
    if fund.get("roce") is not None:
        returns_parts.append(f"ROCE: <b>{_num_or_na(fund['roce'], 1)}%</b>")
    if fund.get("roe") is not None:
        returns_parts.append(f"ROE: <b>{_num_or_na(fund['roe'], 1)}%</b>")
    if returns_parts:
        lines.append("  \u00b7  ".join(returns_parts))
    else:
        lines.append("Full financial statement trends available on Screener.in")
    lines.append("")

    # Analyst forecast (consensus + target + upside) - the forecast value
    forecast_parts = []
    if fund.get("rec_mean") is not None:
        label = _consensus_label(fund)
        forecast_parts.append(f"Consensus: <b>{label}</b> ({fund['rec_mean']:.1f}/5)")
    if fund.get("target_mean") is not None:
        target = float(fund["target_mean"])
        forecast_parts.append(f"Target \u20b9{target:,.0f}")
        if price:
            try:
                upside = (target - float(price)) / float(price) * 100.0
                forecast_parts.append(f"{upside:+.0f}% vs price")
            except (TypeError, ValueError):
                pass
    if forecast_parts:
        lines.append("<b>\U0001F52D ANALYST FORECAST</b>")
        lines.append("  \u00b7  ".join(forecast_parts))
        lines.append("")

    # Top management + top competitors (one-liners)
    officer = _top_officer(fund)
    if officer:
        lines.append(f"\U0001F464 <b>{escape(officer[0])}</b> \u2014 {escape(officer[1])}")
    peers = fund.get("competitors") or []
    if peers:
        names = "  \u00b7  ".join(escape(peer["name"]) for peer in peers[:4] if peer.get("name"))
        if names:
            lines.append(f"\U0001F3E2 Top competitors: {names}")
    if officer or (peers and any(peer.get("name") for peer in peers[:4])):
        lines.append("")

    # Section 5: Shareholding Pattern (QoQ Trend)
    lines.append("<b>\U0001F4BC SHAREHOLDING PATTERN (QoQ TREND)</b>")
    if any(fund.get(key) for key in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        lines.extend(_holding_lines(
            fund,
            (("promoter_pct", "\U0001F451", "Promoter"),
             ("fii_pct", "\U0001F30D", "FII"),
             ("dii_pct", "\U0001F3DB\ufe0f", "DII"),
             ("public_pct", "\U0001F465", "Public")),
        ))
    else:
        lines.append("No shareholding breakdown available.")

    if include_tip:
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_symbol} NSE</i>")
    return lines
