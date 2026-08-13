"""Indian DEEP fundamental report renderer (/fundamentalreport, /fund).

Pure formatting: builds the long multi-section report (valuation, growth &
margins, per-share & scale, balance sheet & cash flow, returns, screener.in
annuals/quarters tables, analyst view, shareholding). No fetching here - the
quote/fund dicts are passed in. The quick card lives in stock_india_card.py,
movers rows in stock_india_movers.py, shared helpers in stock_common.py.
"""
from __future__ import annotations

import re

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import (
    _cr_str,
    _executive_lines,
    _growth_pct_str,
    _num_or_na,
    _pct_str,
    _ratings_text,
    _tech_indicator_lines,
    _wk52_signal,
)
from .stock_india_card import _holding_lines, _price_move_line


def _short_year(label) -> str:
    """'Mar 2022' / 'Jun 2026' -> 'Mar'22' / 'Jun'26'."""
    match = re.search(r"([A-Za-z]+)\s*(\d{4})", label or "")
    if match:
        return f"{match.group(1)}'{match.group(2)[2:]}"
    return label or ""


def _cr_cr(value) -> str:
    """screener.in money in ₹ Crore (the source already reports Cr units)."""
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
    range_tag = _wk52_signal(price, fund)[1]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or range_tag:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        price_line = _price_move_line(quote)
        if price_line:
            lines.append(price_line)
        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            lines.append(
                f"\U0001F4C8 52w Range: {format_money(fund['wk52_low'])} \u2013 {format_money(fund['wk52_high'])}"
            )
        if range_tag:
            lines.append(f"\u26a1 Technicals: {range_tag}")
        lines.append("")

    # Technical indicators: RSI(14), MACD(12,26,9), SMA 50/200
    tech_lines = _tech_indicator_lines(fund, price)
    if tech_lines:
        lines.extend(tech_lines)
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
        # screener.in snapshot (values already in ₹ Crore)
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

    # Section 9: Analyst view & forecast (consensus, ratings, target)
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
            lines.append(f"Forecast Target (Mean): <b>{format_money(target_mean)}</b>{upside_text}")
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

    # Top executives (Yahoo companyOfficers)
    exec_lines = _executive_lines(fund)
    if exec_lines:
        lines.extend(exec_lines)
        lines.append("")

    # Top competitors (screener.in peers by market cap)
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
                + (f" \u2014 {'  \u00b7  '.join(bits)}" if bits else "")
            )
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
