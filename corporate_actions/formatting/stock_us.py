"""US-stock report renderer (USD, no screener.in part).

Renders the deep fundamentals report for a US ticker (/usstock). Everything
here is USD-denominated: prices in $, market cap in $B, cash/debt/FCF in
compact $ units. The Indian renderers live in stock_india.py; shared
number/signal helpers in stock_common.py.
"""
from __future__ import annotations

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import (
    _executive_lines,
    _growth_pct_str,
    _macd_tag,
    _num_or_na,
    _pct_str,
    _ratings_text,
    _rsi_signal,
    _tech_indicator_lines,
    _wk52_signal,
)


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


def _us_movers_lines(fund: dict | None, price=None) -> list[str]:
    """Compact US movers-row fundamentals lines (USD) for /topmovers & co."""
    if not fund:
        return []

    def _num(value, decimals: int) -> str:
        try:
            formatted = f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            return "N/A"
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted

    sig_emoji, range_tag = _wk52_signal(price, fund)
    rsi_tag = _rsi_signal(fund.get("rsi"))
    macd_tag = _macd_tag(fund)
    lines = []

    # Line 1: Signals & Technicals
    l1_parts = []
    if range_tag:
        l1_parts.append(range_tag)
    if rsi_tag:
        l1_parts.append(rsi_tag)
    if macd_tag:
        l1_parts.append(macd_tag)
    if l1_parts:
        lines.append("  \u2022  ".join(l1_parts))

    # Line 2: Valuation & Market Stats
    l2_parts = []
    if fund.get("pe"):
        l2_parts.append(f"P/E {_num(fund['pe'], 1)}")
    else:
        l2_parts.append("P/E N/A (Loss)")
    if fund.get("forward_pe"):
        l2_parts.append(f"Fwd P/E {_num(fund['forward_pe'], 1)}")
    if fund.get("mcap_usd") is not None:
        l2_parts.append(f"MCap ${fund['mcap_usd']:,.2f}B")
    if fund.get("debt_to_equity") is not None:
        l2_parts.append(f"D/E {_num(fund['debt_to_equity'], 2)}")
    if l2_parts:
        lines.append("\U0001F4CA " + "  \u00b7  ".join(l2_parts))

    # Line 3: 52-Week Range & Returns
    l3_parts = []
    if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
        l3_parts.append(
            f"52w Range: {format_money(fund['wk52_low'], 'USD')} \u2013 "
            f"{format_money(fund['wk52_high'], 'USD')}"
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

    return lines


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
    range_tag = _wk52_signal(price, fund)[1]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or range_tag:
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
        if range_tag:
            lines.append("\u26a1 Technicals: " + range_tag)
        lines.append("")

    # TECHNICAL INDICATORS: RSI(14), MACD(12,26,9), SMA 50/200
    tech_lines = _tech_indicator_lines(fund, price)
    if tech_lines:
        lines.extend(tech_lines)
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

    # ANALYST VIEW & FORECAST
    if fund.get("num_analysts") or fund.get("target_mean") or fund.get("rec_mean") or fund.get("rec_trend"):
        lines.append("<b>\U0001F52D ANALYST VIEW & FORECAST</b>")
        ratings = _ratings_text(fund)
        if ratings:
            lines.append(ratings)
        if fund.get("target_mean") is not None:
            target_mean = float(fund["target_mean"])
            upside_text = ""
            if price:
                percent = (target_mean - float(price)) / float(price) * 100
                if percent > 0:
                    upside_text = f"  (<b>+{percent:.0f}%</b> upside)"
                elif percent < 0:
                    upside_text = f"  (<b>{percent:.0f}%</b> downside)"
            lines.append(f"Forecast Target (Mean): <b>{format_money(target_mean, 'USD')}</b>{upside_text}")
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

    # Top executives (Yahoo companyOfficers)
    exec_lines = _executive_lines(fund)
    if exec_lines:
        lines.extend(exec_lines)
        lines.append("")

    if include_tip:
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: schedule this with /schedule add 3h /usstock {raw_symbol} us</i>")
    return lines
