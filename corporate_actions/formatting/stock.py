"""Stock-analysis report renderers: quick card (/stock) + deep report (/fund).

Pure formatting - the quote/fund dicts come from the callers (sources +
fundamentals), these functions only turn them into Telegram HTML.
"""
from __future__ import annotations

import re

from ..core.numbers import format_money
from ..core.text import escape


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


def _num_or_na(value, decimals: int) -> str:
    try:
        formatted = f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _price_move_line(quote: dict) -> str | None:
    """The 'Current Price: ...' line with green/red direction arrow."""
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
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or range_tag or rsi_tag:
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

        if range_tag or rsi_tag:
            technical_bits = [signal for signal in (range_tag, rsi_tag) if signal]
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


def _pct_str(value) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _cr_str(value) -> str:
    if value is None:
        return "N/A"
    try:
        return f"\u20b9{float(value) / 1e7:,.1f}Cr"
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


def _short_year(label) -> str:
    """'Mar 2022' / 'Jun 2026' -> 'Mar'22' / 'Jun'26'."""
    match = re.search(r"([A-Za-z]+)\s*(\d{4})", label or "")
    if match:
        return f"{match.group(1)}'{match.group(2)[2:]}"
    return label or ""


def _cr_cr(value) -> str:
    """screener.in money in \u20b9 Crore (the source already reports Cr units)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "\u2212" if number < 0 else ""
    return f"{sign}\u20b9{abs(number):,.0f}Cr"


def _arrow_pct(percent) -> str:
    """Percent (already in % units) with a green/red arrow."""
    if percent is None:
        return "N/A"
    arrow = "\U0001F7E2\u25b2" if percent >= 0 else "\U0001F534\u25bc"
    return f"{arrow} {percent:+.1f}%"


def _annual_trend_lines(fund: dict) -> list[str]:
    """5-6 fiscal-year P&L trend in a monospace table (screener.in annuals)."""
    annuals = fund.get("annuals") or []
    if not annuals:
        return []
    recent = annuals[-6:]
    lines = ["<b>\U0001F4C8 ANNUAL PERFORMANCE (5-YR TREND)</b>"]
    header = (
        f"{'Fiscal':<7}{'Sales':>10}{'S-YoY':>7}{'OPM':>6}"
        f"{'NetPr':>10}{'NP-YoY':>7}{'EPS':>6}{'ROCE':>6}"
    )
    lines.append(f"<code>{header}</code>")
    for item in recent:
        year = _short_year(item.get("year"))
        sales = f"{item['sales']:,.0f}" if item.get("sales") is not None else "N/A"
        s_growth = f"{item['sales_growth']:+.1f}" if item.get("sales_growth") is not None else "N/A"
        opm = f"{item['opm']:.0f}" if item.get("opm") is not None else "N/A"
        net = f"{item['net_profit']:,.0f}" if item.get("net_profit") is not None else "N/A"
        n_growth = f"{item['profit_growth']:+.1f}" if item.get("profit_growth") is not None else "N/A"
        eps = f"{item['eps']:.1f}" if item.get("eps") is not None else "N/A"
        roce = f"{item['roce']:.0f}" if item.get("roce") is not None else "N/A"
        line = (
            f"{year:<7}{sales:>10}{s_growth:>7}{opm:>6}"
            f"{net:>10}{n_growth:>7}{eps:>6}{roce:>6}"
        )
        lines.append(f"<code>{line}</code>")
    latest = recent[-1]
    bits = []
    if latest.get("sales_growth") is not None:
        bits.append(f"Sales {_arrow_pct(latest['sales_growth'])}")
    if latest.get("profit_growth") is not None:
        bits.append(f"Profit {_arrow_pct(latest['profit_growth'])}")
    if latest.get("interest_coverage") is not None:
        bits.append(f"Int Coverage <b>{_num_or_na(latest['interest_coverage'], 1)}x</b>")
    if bits:
        lines.append("  \u00b7  ".join(bits))
    return lines


def _quarterly_lines(fund: dict) -> list[str]:
    """Last-6-quarters Sales/OPM/Net Profit/EPS in a monospace table."""
    quarters = fund.get("quarters") or []
    if not quarters:
        return []
    recent = quarters[-6:]
    lines = ["<b>\U0001F4C5 QUARTERLY RESULTS (LAST 6 QUARTERS)</b>"]
    header = f"{'Quarter':<8}{'Sales':>10}{'OPM':>6}{'NetPr':>10}{'EPS':>7}"
    lines.append(f"<code>{header}</code>")
    for item in recent:
        quarter = _short_year(item.get("quarter"))
        sales = f"{item['sales']:,.0f}" if item.get("sales") is not None else "N/A"
        opm = f"{item['opm']:.0f}" if item.get("opm") is not None else "N/A"
        net = f"{item['net_profit']:,.0f}" if item.get("net_profit") is not None else "N/A"
        eps = f"{item['eps']:.1f}" if item.get("eps") is not None else "N/A"
        line = f"{quarter:<8}{sales:>10}{opm:>6}{net:>10}{eps:>7}"
        lines.append(f"<code>{line}</code>")
    latest, previous = recent[-1], recent[-2] if len(recent) >= 2 else None
    bits = []
    if (
        previous
        and latest.get("net_profit") is not None
        and previous.get("net_profit") is not None
        and previous["net_profit"] != 0
    ):
        qoq = (latest["net_profit"] - previous["net_profit"]) / abs(previous["net_profit"]) * 100
        bits.append(f"NP QoQ {_arrow_pct(qoq)}")
    if latest.get("opm") is not None:
        bits.append(f"OPM {latest['opm']:.0f}%")
    if latest.get("eps") is not None:
        bits.append(f"EPS {_num_or_na(latest['eps'], 2)}")
    if bits:
        lines.append("  \u00b7  ".join(bits))
    return lines


def _fund_report_lines(raw_symbol, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the deep /fund fundamental report for one symbol."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    company_name = quote.get("name") or raw_symbol

    lines = []
    sector_name = escape(fund.get("sector") or "Indian Equity")
    industry_name = escape(fund.get("industry") or "")
    label_prefix = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {label_prefix}<b>{escape(company_name.upper())}</b> (<code>{escape(raw_symbol)}</code>)"
    )
    if industry_name:
        lines.append(f"Sector: <i>{sector_name}</i>  \u00b7  Industry: <i>{industry_name}</i>")
    else:
        lines.append(f"Sector: <i>{sector_name}</i>")
    lines.append("")

    # Section 1: Price & movement
    technical_bits = [signal for signal in (_wk52_signal(price, fund)[1], _rsi_signal(fund.get("rsi"))) if signal]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or technical_bits:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        price_line = _price_move_line(quote)
        if price_line:
            lines.append(price_line)
        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            lines.append(
                f"\U0001F4C8 52w Range: {format_money(fund['wk52_low'])} \u2013 {format_money(fund['wk52_high'])}"
            )
        if technical_bits:
            lines.append(f"\u26a1 Technicals: {'  •  '.join(technical_bits)}")
        lines.append("")

    # Section 2: Valuation
    lines.append("<b>\U0001F3F7\ufe0f VALUATION</b>")
    valuation_parts = []
    if fund.get("pe"):
        valuation_parts.append(f"P/E: <b>{_num_or_na(fund['pe'], 1)}</b>")
    else:
        valuation_parts.append("P/E: <b>N/A (Loss)</b>")
    if fund.get("forward_pe"):
        valuation_parts.append(f"Fwd P/E: <b>{_num_or_na(fund['forward_pe'], 1)}</b>")
    if fund.get("sector_pe"):
        valuation_parts.append(f"Sector P/E: <b>{_num_or_na(fund['sector_pe'], 1)}</b>")
    if fund.get("price_to_book"):
        valuation_parts.append(f"P/B: <b>{_num_or_na(fund['price_to_book'], 2)}</b>")
    if fund.get("price_to_sales"):
        valuation_parts.append(f"P/S: <b>{_num_or_na(fund['price_to_sales'], 2)}</b>")
    if fund.get("div_yield") is not None:
        valuation_parts.append(f"Div Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")
    if fund.get("beta") is not None:
        valuation_parts.append(f"Beta: <b>{_num_or_na(fund['beta'], 2)}</b>")
    lines.append("  \u00b7  ".join(valuation_parts))
    if fund.get("market_cap") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")
    elif fund.get("mcap_cr") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['mcap_cr']:,.0f}Cr</b>")
    if fund.get("enterprise_value") is not None:
        lines.append(f"Enterprise Value: <b>{_cr_str(fund['enterprise_value'])}</b>")
    lines.append("")

    # Section 3: Growth & margins (YoY)
    growth_parts = []
    if fund.get("earnings_growth") is not None:
        growth_parts.append(f"Earnings: <b>{_growth_pct_str(fund['earnings_growth'])}</b>")
    if fund.get("revenue_growth") is not None:
        growth_parts.append(f"Revenue: <b>{_growth_pct_str(fund['revenue_growth'])}</b>")
    margin_parts = []
    if fund.get("gross_margin") is not None:
        margin_parts.append(f"Gross: <b>{_pct_str(fund['gross_margin'])}</b>")
    if fund.get("ebitda_margin") is not None:
        margin_parts.append(f"EBITDA: <b>{_pct_str(fund['ebitda_margin'])}</b>")
    if fund.get("operating_margin") is not None:
        margin_parts.append(f"Operating: <b>{_pct_str(fund['operating_margin'])}</b>")
    if fund.get("profit_margin") is not None:
        margin_parts.append(f"Net: <b>{_pct_str(fund['profit_margin'])}</b>")
    if growth_parts or margin_parts:
        lines.append("<b>\U0001F4C8 GROWTH & MARGINS (YoY)</b>")
        if growth_parts:
            lines.append("  \u00b7  ".join(growth_parts))
        if margin_parts:
            lines.append("  \u00b7  ".join(margin_parts))
        lines.append("")

    # Section 4: Per-share & scale
    per_share_parts = []
    if fund.get("trailing_eps") is not None:
        per_share_parts.append(f"EPS(TTM): <b>{_num_or_na(fund['trailing_eps'], 2)}</b>")
    if fund.get("forward_eps") is not None:
        per_share_parts.append(f"EPS(Fwd): <b>{_num_or_na(fund['forward_eps'], 2)}</b>")
    if fund.get("revenue_per_share") is not None:
        per_share_parts.append(f"Rev/Share: <b>{_num_or_na(fund['revenue_per_share'], 2)}</b>")
    if fund.get("book_value") is not None:
        per_share_parts.append(f"Book Value: <b>{_num_or_na(fund['book_value'], 2)}</b>")
    if fund.get("cash_per_share") is not None:
        per_share_parts.append(f"Cash/Share: <b>{_num_or_na(fund['cash_per_share'], 2)}</b>")
    if per_share_parts or fund.get("shares_outstanding") is not None:
        lines.append("<b>\U0001F4BC PER-SHARE & SCALE</b>")
        if per_share_parts:
            lines.append("  \u00b7  ".join(per_share_parts))
        if fund.get("shares_outstanding") is not None:
            lines.append(f"Shares Outstanding: <b>{fund['shares_outstanding'] / 1e7:,.2f}Cr</b>")
        lines.append("")

    # Section 5: Balance sheet & cash flow
    balance_sheet = fund.get("balance_sheet") or {}
    cash_flow = fund.get("cash_flow") or {}
    balance_sheet_parts = []
    if fund.get("debt_to_equity") is not None:
        balance_sheet_parts.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}</b>")
    if fund.get("current_ratio") is not None:
        balance_sheet_parts.append(f"Current Ratio: <b>{_num_or_na(fund['current_ratio'], 2)}</b>")
    if fund.get("quick_ratio") is not None:
        balance_sheet_parts.append(f"Quick Ratio: <b>{_num_or_na(fund['quick_ratio'], 2)}</b>")
    if fund.get("interest_coverage_ratio") is not None:
        balance_sheet_parts.append(f"Int Coverage: <b>{_num_or_na(fund['interest_coverage_ratio'], 1)}x</b>")
    if (
        balance_sheet_parts
        or fund.get("total_cash") is not None
        or fund.get("total_debt") is not None
        or balance_sheet
        or cash_flow
    ):
        lines.append("<b>\U0001F4C9 BALANCE SHEET & CASH FLOW</b>")
        if balance_sheet_parts:
            lines.append("  \u00b7  ".join(balance_sheet_parts))
        if fund.get("total_cash") is not None or fund.get("total_debt") is not None:
            lines.append(
                f"Cash: <b>{_cr_str(fund.get('total_cash'))}</b>  \u00b7  Debt: <b>{_cr_str(fund.get('total_debt'))}</b>"
            )
        # screener.in snapshot (values already in \u20b9 Crore)
        bs_bits = []
        if balance_sheet.get("net_worth") is not None:
            bs_bits.append(f"Net Worth: <b>{_cr_cr(balance_sheet['net_worth'])}</b>")
        if balance_sheet.get("borrowings") is not None:
            bs_bits.append(f"Borrowings: <b>{_cr_cr(balance_sheet['borrowings'])}</b>")
        if balance_sheet.get("total_assets") is not None:
            bs_bits.append(f"Total Assets: <b>{_cr_cr(balance_sheet['total_assets'])}</b>")
        if bs_bits:
            year_tag = f" <i>(FY {_short_year(balance_sheet.get('year'))})</i>" if balance_sheet.get("year") else ""
            lines.append("\U0001F7E2 " + "  \u00b7  ".join(bs_bits) + year_tag)
        cf_bits = []
        if cash_flow.get("cfo") is not None:
            cf_bits.append(f"CFO {_cr_cr(cash_flow['cfo'])}")
        if cash_flow.get("cfi") is not None:
            cf_bits.append(f"CFI {_cr_cr(cash_flow['cfi'])}")
        if cash_flow.get("cff") is not None:
            cf_bits.append(f"CFF {_cr_cr(cash_flow['cff'])}")
        if cash_flow.get("net_cash_flow") is not None:
            cf_bits.append(f"NCF {_cr_cr(cash_flow['net_cash_flow'])}")
        if cash_flow.get("free_cash_flow") is not None:
            cf_bits.append(f"FCF {_cr_cr(cash_flow['free_cash_flow'])}")
        if cf_bits:
            year_tag = f" <i>(FY {_short_year(cash_flow.get('year'))})</i>" if cash_flow.get("year") else ""
            lines.append("\U0001F4B5 " + "  \u00b7  ".join(cf_bits) + year_tag)
        cash_flow_parts = []
        if fund.get("free_cashflow") is not None:
            cash_flow_parts.append(f"Free Cash Flow: <b>{_cr_str(fund['free_cashflow'])}</b>")
        if fund.get("operating_cashflow") is not None:
            cash_flow_parts.append(f"Operating Cash Flow: <b>{_cr_str(fund['operating_cashflow'])}</b>")
        if cash_flow_parts:
            lines.append("  \u00b7  ".join(cash_flow_parts))
        lines.append("")

    # Section 6: Returns
    returns_parts = []
    if fund.get("roce") is not None:
        returns_parts.append(f"ROCE: <b>{_num_or_na(fund['roce'], 1)}%</b>")
    if fund.get("roe") is not None:
        returns_parts.append(f"ROE: <b>{_num_or_na(fund['roe'], 1)}%</b>")
    if returns_parts:
        lines.append("<b>\U0001F3AF RETURNS</b>")
        lines.append("  \u00b7  ".join(returns_parts))
        lines.append("")

    # Section 7: Annual P&L trend (screener.in)
    annual_lines = _annual_trend_lines(fund)
    if annual_lines:
        lines.extend(annual_lines)
        lines.append("")

    # Section 8: Quarterly results (screener.in)
    quarter_lines = _quarterly_lines(fund)
    if quarter_lines:
        lines.extend(quarter_lines)
        lines.append("")

    # Section 9: Analyst view
    if fund.get("num_analysts") or fund.get("target_mean"):
        lines.append("<b>\U0001F52D ANALYST VIEW</b>")
        if fund.get("target_mean") is not None:
            target_mean = float(fund["target_mean"])
            upside_text = ""
            if price:
                percent = (target_mean - float(price)) / float(price) * 100
                if percent > 0:
                    upside_text = f"  (<b>+{percent:.0f}%</b> upside)"
                elif percent < 0:
                    upside_text = f"  (<b>{percent:.0f}%</b> downside)"
            lines.append(f"Target (Mean): <b>{format_money(target_mean)}</b>{upside_text}")
        target_range_parts = []
        if fund.get("target_high") is not None:
            target_range_parts.append(f"High {format_money(fund['target_high'])}")
        if fund.get("target_low") is not None:
            target_range_parts.append(f"Low {format_money(fund['target_low'])}")
        if target_range_parts:
            lines.append("  " + "  \u00b7  ".join(target_range_parts))
        if fund.get("num_analysts"):
            lines.append(f"Analysts Covering: <b>{fund['num_analysts']}</b>")
        lines.append("")

    # Section 10: Shareholding QoQ trend
    lines.append("<b>\U0001F4BC SHAREHOLDING (QoQ TREND)</b>")
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


def _usd_compact(value) -> str:
    """Compact USD money: '$2.90T' / '$1.24B' / '$850M' (None -> 'N/A')."""
    if value is None:
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if magnitude >= 1e12:
        return f"{sign}${magnitude / 1e12:,.2f}T"
    if magnitude >= 1e9:
        return f"{sign}${magnitude / 1e9:,.2f}B"
    if magnitude >= 1e6:
        return f"{sign}${magnitude / 1e6:,.1f}M"
    return f"{sign}${magnitude:,.0f}"


def _us_price_move_line(quote: dict) -> str | None:
    """The 'Current Price: ...' line for a US quote (USD, green/red arrow)."""
    price = quote.get("price")
    if price is None:
        return None
    change_pct = quote.get("change_pct")
    price_str = format_money(price, "USD")
    if change_pct is not None:
        sign = "+" if change_pct >= 0 else ""
        arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
        color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
        return (
            f"Current Price: <b>{price_str}</b>  {color_icon}{arrow} "
            f"<b>{sign}{change_pct:.2f}%</b>"
        )
    return f"Current Price: <b>{price_str}</b>"


def _us_stock_lines(raw_symbol, quote, fund, include_tip=True, label="") -> list[str]:
    """Deep fundamentals report for a US ticker (USD, no screener.in part).

    Uses the same field names as the Indian report (pe, forward_pe,
    debt_to_equity, div_yield, margins, ...) but prices/money in dollars and
    market cap in $B. `quote` comes from get_quote('US', symbol).
    """
    price = quote.get("price")
    company_name = quote.get("name") or raw_symbol
    lines = []
    sector_name = escape(fund.get("sector") or "US Equity")
    industry_name = escape(fund.get("industry") or "")
    label_prefix = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {label_prefix}<b>{escape(company_name.upper())}</b> "
        f"(<code>{escape(raw_symbol)}</code>)"
    )
    if industry_name:
        lines.append(f"Sector: <i>{sector_name}</i>  \u00b7  Industry: <i>{industry_name}</i>")
    else:
        lines.append(f"Sector: <i>{sector_name}</i>")
    lines.append("")

    # PRICE & MOVEMENT
    technical_bits = [
        signal for signal in (_wk52_signal(price, fund)[1], _rsi_signal(fund.get("rsi"))) if signal
    ]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or technical_bits:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        price_line = _us_price_move_line(quote)
        if price_line:
            lines.append(price_line)
        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            lines.append(
                f"\U0001F4C8 52w Range: {format_money(fund['wk52_low'], 'USD')} \u2013 "
                f"{format_money(fund['wk52_high'], 'USD')}"
            )
            if price:
                try:
                    distance_from_low = ((float(price) - fund["wk52_low"]) / fund["wk52_low"]) * 100
                    distance_from_high = ((fund["wk52_high"] - float(price)) / fund["wk52_high"]) * 100
                    lines.append(
                        f"\U0001F4CD Distance: +{distance_from_low:.1f}% from 52w Low  \u00b7  "
                        f"-{distance_from_high:.1f}% from 52w High"
                    )
                except (ValueError, TypeError, ZeroDivisionError):
                    pass
        if technical_bits:
            lines.append(f"\u26a1 Technicals: {'  \u2022  '.join(technical_bits)}")
        lines.append("")

    # VALUATION
    lines.append("<b>\U0001F3F7\ufe0f VALUATION</b>")
    valuation_parts = []
    if fund.get("pe"):
        valuation_parts.append(f"P/E: <b>{_num_or_na(fund['pe'], 1)}</b>")
    else:
        valuation_parts.append("P/E: <b>N/A (Loss)</b>")
    if fund.get("forward_pe"):
        valuation_parts.append(f"Fwd P/E: <b>{_num_or_na(fund['forward_pe'], 1)}</b>")
    if fund.get("peg") is not None:
        valuation_parts.append(f"PEG: <b>{_num_or_na(fund['peg'], 2)}</b>")
    if fund.get("price_to_book"):
        valuation_parts.append(f"P/B: <b>{_num_or_na(fund['price_to_book'], 2)}</b>")
    if fund.get("price_to_sales"):
        valuation_parts.append(f"P/S: <b>{_num_or_na(fund['price_to_sales'], 2)}</b>")
    if fund.get("div_yield") is not None:
        valuation_parts.append(f"Div Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")
    if fund.get("beta") is not None:
        valuation_parts.append(f"Beta: <b>{_num_or_na(fund['beta'], 2)}</b>")
    lines.append("  \u00b7  ".join(valuation_parts))
    if fund.get("mcap_usd") is not None:
        lines.append(f"Market Cap: <b>${fund['mcap_usd']:,.2f}B</b>")
    if fund.get("enterprise_value") is not None:
        lines.append(f"Enterprise Value: <b>{_usd_compact(fund['enterprise_value'])}</b>")
    lines.append("")

    # GROWTH & MARGINS (YoY)
    growth_parts = []
    if fund.get("earnings_growth") is not None:
        growth_parts.append(f"Earnings: <b>{_growth_pct_str(fund['earnings_growth'])}</b>")
    if fund.get("revenue_growth") is not None:
        growth_parts.append(f"Revenue: <b>{_growth_pct_str(fund['revenue_growth'])}</b>")
    margin_parts = []
    if fund.get("gross_margin") is not None:
        margin_parts.append(f"Gross: <b>{_pct_str(fund['gross_margin'])}</b>")
    if fund.get("ebitda_margin") is not None:
        margin_parts.append(f"EBITDA: <b>{_pct_str(fund['ebitda_margin'])}</b>")
    if fund.get("operating_margin") is not None:
        margin_parts.append(f"Operating: <b>{_pct_str(fund['operating_margin'])}</b>")
    if fund.get("profit_margin") is not None:
        margin_parts.append(f"Net: <b>{_pct_str(fund['profit_margin'])}</b>")
    if growth_parts or margin_parts:
        lines.append("<b>\U0001F4C8 GROWTH & MARGINS (YoY)</b>")
        if growth_parts:
            lines.append("  \u00b7  ".join(growth_parts))
        if margin_parts:
            lines.append("  \u00b7  ".join(margin_parts))
        lines.append("")

    # PER-SHARE & SCALE
    per_share_parts = []
    if fund.get("trailing_eps") is not None:
        per_share_parts.append(f"EPS(TTM): <b>${_num_or_na(fund['trailing_eps'], 2)}</b>")
    if fund.get("forward_eps") is not None:
        per_share_parts.append(f"EPS(Fwd): <b>${_num_or_na(fund['forward_eps'], 2)}</b>")
    if fund.get("revenue_per_share") is not None:
        per_share_parts.append(f"Rev/Share: <b>${_num_or_na(fund['revenue_per_share'], 2)}</b>")
    if fund.get("book_value") is not None:
        per_share_parts.append(f"Book Value: <b>${_num_or_na(fund['book_value'], 2)}</b>")
    if fund.get("cash_per_share") is not None:
        per_share_parts.append(f"Cash/Share: <b>${_num_or_na(fund['cash_per_share'], 2)}</b>")
    if per_share_parts or fund.get("shares_outstanding") is not None:
        lines.append("<b>\U0001F4BC PER-SHARE & SCALE</b>")
        if per_share_parts:
            lines.append("  \u00b7  ".join(per_share_parts))
        if fund.get("shares_outstanding") is not None:
            lines.append(f"Shares Outstanding: <b>{fund['shares_outstanding'] / 1e9:,.2f}B</b>")
        if fund.get("employees"):
            lines.append(f"Employees: <b>{fund['employees']:,}</b>")
        lines.append("")

    # BALANCE SHEET & CASH FLOW
    balance_sheet_parts = []
    if fund.get("debt_to_equity") is not None:
        balance_sheet_parts.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}</b>")
    if fund.get("current_ratio") is not None:
        balance_sheet_parts.append(f"Current Ratio: <b>{_num_or_na(fund['current_ratio'], 2)}</b>")
    if fund.get("quick_ratio") is not None:
        balance_sheet_parts.append(f"Quick Ratio: <b>{_num_or_na(fund['quick_ratio'], 2)}</b>")
    if balance_sheet_parts or fund.get("total_cash") is not None or fund.get("total_debt") is not None:
        lines.append("<b>\U0001F4C9 BALANCE SHEET & CASH FLOW</b>")
        if balance_sheet_parts:
            lines.append("  \u00b7  ".join(balance_sheet_parts))
        if fund.get("total_cash") is not None or fund.get("total_debt") is not None:
            lines.append(
                f"Cash: <b>{_usd_compact(fund.get('total_cash'))}</b>  \u00b7  "
                f"Debt: <b>{_usd_compact(fund.get('total_debt'))}</b>"
            )
        cash_flow_parts = []
        if fund.get("free_cashflow") is not None:
            cash_flow_parts.append(f"Free Cash Flow: <b>{_usd_compact(fund['free_cashflow'])}</b>")
        if fund.get("operating_cashflow") is not None:
            cash_flow_parts.append(f"Operating Cash Flow: <b>{_usd_compact(fund['operating_cashflow'])}</b>")
        if cash_flow_parts:
            lines.append("  \u00b7  ".join(cash_flow_parts))
        lines.append("")

    # RETURNS
    returns_parts = []
    if fund.get("roe") is not None:
        returns_parts.append(f"ROE: <b>{_num_or_na(fund['roe'], 1)}%</b>")
    if fund.get("roce") is not None:
        returns_parts.append(f"ROCE: <b>{_num_or_na(fund['roce'], 1)}%</b>")
    if returns_parts:
        lines.append("<b>\U0001F3AF RETURNS</b>")
        lines.append("  \u00b7  ".join(returns_parts))
        lines.append("")

    # ANALYST VIEW
    if fund.get("num_analysts") or fund.get("target_mean"):
        lines.append("<b>\U0001F52D ANALYST VIEW</b>")
        if fund.get("target_mean") is not None:
            target_mean = float(fund["target_mean"])
            upside_text = ""
            if price:
                percent = (target_mean - float(price)) / float(price) * 100
                if percent > 0:
                    upside_text = f"  (<b>+{percent:.0f}%</b> upside)"
                elif percent < 0:
                    upside_text = f"  (<b>{percent:.0f}%</b> downside)"
            lines.append(f"Target (Mean): <b>{format_money(target_mean, 'USD')}</b>{upside_text}")
        target_range_parts = []
        if fund.get("target_high") is not None:
            target_range_parts.append(f"High {format_money(fund['target_high'], 'USD')}")
        if fund.get("target_low") is not None:
            target_range_parts.append(f"Low {format_money(fund['target_low'], 'USD')}")
        if target_range_parts:
            lines.append("  " + "  \u00b7  ".join(target_range_parts))
        if fund.get("num_analysts"):
            lines.append(f"Analysts Covering: <b>{fund['num_analysts']}</b>")
        lines.append("")

    if include_tip:
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: schedule this with /schedule add 3h /usstock {raw_symbol} us</i>")
    return lines
