"""Stock-analysis report renderers: quick card (/stock) + deep report (/fund).

Pure formatting - the quote/fund dicts come from the callers (sources +
fundamentals), these functions only turn them into Telegram HTML.
"""
from __future__ import annotations

from ..core.numbers import fmt_money
from ..core.text import escape


def _wk52_signal(price, fund: dict | None) -> tuple:
    """Return (signal_emoji, range_tag) based on 52-week position of price."""
    if not fund:
        return "", ""
    lo = fund.get("wk52_low")
    hi = fund.get("wk52_high")
    if lo is None or hi is None or price is None:
        return "", ""
    try:
        price = float(price)
        lo = float(lo)
        hi = float(hi)
    except (TypeError, ValueError):
        return "", ""
    spread = hi - lo
    if spread <= 0:
        return "", ""
    pct_pos = (price - lo) / spread  # 0.0 = at 52W low, 1.0 = at 52W high
    if pct_pos <= 0.15:
        return "\u2705", "\U0001F7E2 Near 52W Low"
    if pct_pos <= 0.35:
        return "\U0001F4C8", "\U0001F7E2 Low Zone"
    if pct_pos >= 0.85:
        return "\U0001F6AB", "\U0001F534 Near 52W High"
    if pct_pos >= 0.65:
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

    def _num(value, nd: int) -> str:
        s = f"{value:.{nd}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

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
            f"52w Range: {fmt_money(fund['wk52_low'])} \u2013 "
            f"{fmt_money(fund['wk52_high'])}"
        )
    if fund.get("div_yield") is not None:
        l3_parts.append(f"Div Yield: {_num(fund['div_yield'], 2)}%")
    if fund.get("roce") is not None or fund.get("roe") is not None:
        r_bits = []
        if fund.get("roce"):
            r_bits.append(f"ROCE {_num(fund['roce'], 1)}%")
        if fund.get("roe"):
            r_bits.append(f"ROE {_num(fund['roe'], 1)}%")
        l3_parts.append(" ".join(r_bits))
    if l3_parts:
        lines.append("\U0001F4C8 " + "  \u00b7  ".join(l3_parts))

    # Line 4: Shareholding Pattern (with QoQ trends!)
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        h_bits = []
        for key, label in (
            ("promoter_pct", "Prom"),
            ("fii_pct", "FII"),
            ("dii_pct", "DII"),
            ("public_pct", "Pub"),
        ):
            if fund.get(key):
                h_bits.append(f"{label} {escape(fund[key])}")
        lines.append("\U0001F4BC Holding (QoQ): " + "  \u00b7  ".join(h_bits))

    return lines


def _num_or_na(value, nd: int) -> str:
    try:
        s = f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return "N/A"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _price_move_line(quote: dict) -> str | None:
    """The 'Current Price: ...' line with green/red direction arrow."""
    price = quote.get("price")
    if price is None:
        return None
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    p_str = fmt_money(price)
    if change_pct is not None:
        sign = "+" if change_pct >= 0 else ""
        abs_str = f" ({sign}{fmt_money(change_abs)})" if change_abs is not None else ""
        arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
        color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
        return (
            f"Current Price: <b>{p_str}</b>  {color_icon}{arrow} "
            f"<b>{sign}{change_pct:.2f}%</b>{abs_str}"
        )
    return f"Current Price: <b>{p_str}</b>"


def _holding_lines(fund: dict, label_map: tuple) -> list[str]:
    out = []
    for key, emoji, label in label_map:
        if fund.get(key):
            out.append(f"{emoji} {label}: {escape(fund[key])}")
    return out


def _stock_summary_lines(raw_sym, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the compact /stock summary card for one symbol."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    comp_name = quote.get("name") or raw_sym

    lines = []
    sec_name = escape(fund.get("sector") or "Indian Equity")
    lbl = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {lbl}<b>{escape(comp_name.upper())}</b> (<code>{escape(raw_sym)}</code>)"
    )
    lines.append(f"Sector: <i>{sec_name}</i>")
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
            hi, lo = fund["wk52_high"], fund["wk52_low"]
            lines.append(f"\U0001F4C8 52w Range: {fmt_money(lo)} \u2013 {fmt_money(hi)}")
            if price:
                try:
                    dist_lo = ((float(price) - lo) / lo) * 100
                    dist_hi = ((hi - float(price)) / hi) * 100
                    lines.append(f"📍 Distance: +{dist_lo:.1f}% from 52w Low  \u00b7  -{dist_hi:.1f}% from 52w High")
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

        if range_tag or rsi_tag:
            t_bits = [b for b in (range_tag, rsi_tag) if b]
            lines.append(f"\u26a1 Technicals: {'  •  '.join(t_bits)}")
        lines.append("")

    # Section 2: Valuation & Ratios
    lines.append("<b>\U0001F3F7\ufe0f VALUATION & RATIOS</b>")
    val_parts = []
    if fund.get("pe"):
        val_parts.append(f"Stock P/E: <b>{_num_or_na(fund['pe'], 1)}</b>")
    else:
        val_parts.append("Stock P/E: <b>N/A (Loss)</b>")

    if fund.get("sector_pe"):
        val_parts.append(f"Sec P/E: <b>{_num_or_na(fund['sector_pe'], 1)}</b>")

    if fund.get("market_cap") is not None:
        val_parts.append(f"MCap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")

    if fund.get("debt_to_equity") is not None:
        val_parts.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}</b>")

    if fund.get("div_yield") is not None:
        val_parts.append(f"Div Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")

    lines.append("  \u00b7  ".join(val_parts))
    lines.append("")

    # Section 3: Profitability & Returns
    lines.append("<b>\U0001F3AF PROFITABILITY & RETURNS</b>")
    ret_parts = []
    if fund.get("roce") is not None:
        ret_parts.append(f"ROCE: <b>{_num_or_na(fund['roce'], 1)}%</b>")
    if fund.get("roe") is not None:
        ret_parts.append(f"ROE: <b>{_num_or_na(fund['roe'], 1)}%</b>")
    if ret_parts:
        lines.append("  \u00b7  ".join(ret_parts))
    else:
        lines.append("Full financial statement trends available on Screener.in")
    lines.append("")

    # Section 4: Shareholding Pattern (QoQ Trend)
    lines.append("<b>\U0001F4BC SHAREHOLDING PATTERN (QoQ TREND)</b>")
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
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
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_sym} NSE</i>")
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
    s = _pct_str(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return s
    arrow = "\U0001F7E2\u25b2" if v >= 0 else "\U0001F534\u25bc"
    return f"{arrow} {s}"


def _fund_report_lines(raw_sym, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the deep /fund fundamental report for one symbol."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    change_abs = quote.get("change")
    comp_name = quote.get("name") or raw_sym

    lines = []
    sec_name = escape(fund.get("sector") or "Indian Equity")
    ind_name = escape(fund.get("industry") or "")
    lbl = f"{label} " if label else ""
    lines.append(
        f"\U0001F4CA {lbl}<b>{escape(comp_name.upper())}</b> (<code>{escape(raw_sym)}</code>)"
    )
    if ind_name:
        lines.append(f"Sector: <i>{sec_name}</i>  \u00b7  Industry: <i>{ind_name}</i>")
    else:
        lines.append(f"Sector: <i>{sec_name}</i>")
    lines.append("")

    # Section 1: Price & movement
    t_bits = [b for b in (_wk52_signal(price, fund)[1], _rsi_signal(fund.get("rsi"))) if b]
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or t_bits:
        lines.append("<b>\U0001F4B0 PRICE & MOVEMENT</b>")
        price_line = _price_move_line(quote)
        if price_line:
            lines.append(price_line)
        if fund.get("wk52_high") is not None and fund.get("wk52_low") is not None:
            lines.append(
                f"\U0001F4C8 52w Range: {fmt_money(fund['wk52_low'])} \u2013 {fmt_money(fund['wk52_high'])}"
            )
        if t_bits:
            lines.append(f"\u26a1 Technicals: {'  •  '.join(t_bits)}")
        lines.append("")

    # Section 2: Valuation
    lines.append("<b>\U0001F3F7\ufe0f VALUATION</b>")
    val = []
    if fund.get("pe"):
        val.append(f"P/E: <b>{_num_or_na(fund['pe'], 1)}</b>")
    else:
        val.append("P/E: <b>N/A (Loss)</b>")
    if fund.get("forward_pe"):
        val.append(f"Fwd P/E: <b>{_num_or_na(fund['forward_pe'], 1)}</b>")
    if fund.get("sector_pe"):
        val.append(f"Sector P/E: <b>{_num_or_na(fund['sector_pe'], 1)}</b>")
    if fund.get("price_to_book"):
        val.append(f"P/B: <b>{_num_or_na(fund['price_to_book'], 2)}</b>")
    if fund.get("price_to_sales"):
        val.append(f"P/S: <b>{_num_or_na(fund['price_to_sales'], 2)}</b>")
    if fund.get("div_yield") is not None:
        val.append(f"Div Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")
    if fund.get("beta") is not None:
        val.append(f"Beta: <b>{_num_or_na(fund['beta'], 2)}</b>")
    lines.append("  \u00b7  ".join(val))
    if fund.get("market_cap") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['market_cap']:,.0f}Cr</b>")
    elif fund.get("mcap_cr") is not None:
        lines.append(f"Market Cap: <b>\u20b9{fund['mcap_cr']:,.0f}Cr</b>")
    if fund.get("enterprise_value") is not None:
        lines.append(f"Enterprise Value: <b>{_cr_str(fund['enterprise_value'])}</b>")
    lines.append("")

    # Section 3: Growth & margins (YoY)
    grow = []
    if fund.get("earnings_growth") is not None:
        grow.append(f"Earnings: <b>{_growth_pct_str(fund['earnings_growth'])}</b>")
    if fund.get("revenue_growth") is not None:
        grow.append(f"Revenue: <b>{_growth_pct_str(fund['revenue_growth'])}</b>")
    marg = []
    if fund.get("gross_margin") is not None:
        marg.append(f"Gross: <b>{_pct_str(fund['gross_margin'])}</b>")
    if fund.get("ebitda_margin") is not None:
        marg.append(f"EBITDA: <b>{_pct_str(fund['ebitda_margin'])}</b>")
    if fund.get("operating_margin") is not None:
        marg.append(f"Operating: <b>{_pct_str(fund['operating_margin'])}</b>")
    if fund.get("profit_margin") is not None:
        marg.append(f"Net: <b>{_pct_str(fund['profit_margin'])}</b>")
    if grow or marg:
        lines.append("<b>\U0001F4C8 GROWTH & MARGINS (YoY)</b>")
        if grow:
            lines.append("  \u00b7  ".join(grow))
        if marg:
            lines.append("  \u00b7  ".join(marg))
        lines.append("")

    # Section 4: Per-share & scale
    per = []
    if fund.get("trailing_eps") is not None:
        per.append(f"EPS(TTM): <b>{_num_or_na(fund['trailing_eps'], 2)}</b>")
    if fund.get("forward_eps") is not None:
        per.append(f"EPS(Fwd): <b>{_num_or_na(fund['forward_eps'], 2)}</b>")
    if fund.get("revenue_per_share") is not None:
        per.append(f"Rev/Share: <b>{_num_or_na(fund['revenue_per_share'], 2)}</b>")
    if fund.get("book_value") is not None:
        per.append(f"Book Value: <b>{_num_or_na(fund['book_value'], 2)}</b>")
    if fund.get("cash_per_share") is not None:
        per.append(f"Cash/Share: <b>{_num_or_na(fund['cash_per_share'], 2)}</b>")
    if per or fund.get("shares_outstanding") is not None:
        lines.append("<b>\U0001F4BC PER-SHARE & SCALE</b>")
        if per:
            lines.append("  \u00b7  ".join(per))
        if fund.get("shares_outstanding") is not None:
            lines.append(f"Shares Outstanding: <b>{fund['shares_outstanding'] / 1e7:,.2f}Cr</b>")
        lines.append("")

    # Section 5: Balance sheet
    bs = []
    if fund.get("debt_to_equity") is not None:
        bs.append(f"D/E: <b>{_num_or_na(fund['debt_to_equity'], 2)}</b>")
    if fund.get("current_ratio") is not None:
        bs.append(f"Current Ratio: <b>{_num_or_na(fund['current_ratio'], 2)}</b>")
    if fund.get("quick_ratio") is not None:
        bs.append(f"Quick Ratio: <b>{_num_or_na(fund['quick_ratio'], 2)}</b>")
    if bs or fund.get("total_cash") is not None or fund.get("total_debt") is not None:
        lines.append("<b>\U0001F4C9 BALANCE SHEET</b>")
        if bs:
            lines.append("  \u00b7  ".join(bs))
        if fund.get("total_cash") is not None or fund.get("total_debt") is not None:
            lines.append(
                f"Cash: <b>{_cr_str(fund.get('total_cash'))}</b>  \u00b7  Debt: <b>{_cr_str(fund.get('total_debt'))}</b>"
            )
        cf = []
        if fund.get("free_cashflow") is not None:
            cf.append(f"Free Cash Flow: <b>{_cr_str(fund['free_cashflow'])}</b>")
        if fund.get("operating_cashflow") is not None:
            cf.append(f"Operating Cash Flow: <b>{_cr_str(fund['operating_cashflow'])}</b>")
        if cf:
            lines.append("  \u00b7  ".join(cf))
        lines.append("")

    # Section 6: Returns
    ret = []
    if fund.get("roce") is not None:
        ret.append(f"ROCE: <b>{_num_or_na(fund['roce'], 1)}%</b>")
    if fund.get("roe") is not None:
        ret.append(f"ROE: <b>{_num_or_na(fund['roe'], 1)}%</b>")
    if ret:
        lines.append("<b>\U0001F3AF RETURNS</b>")
        lines.append("  \u00b7  ".join(ret))
        lines.append("")

    # Section 7: Analyst view
    if fund.get("num_analysts") or fund.get("target_mean"):
        lines.append("<b>\U0001F52D ANALYST VIEW</b>")
        if fund.get("target_mean") is not None:
            tm = float(fund["target_mean"])
            ups = ""
            if price:
                pct = (tm - float(price)) / float(price) * 100
                if pct > 0:
                    ups = f"  (<b>+{pct:.0f}%</b> upside)"
                elif pct < 0:
                    ups = f"  (<b>{pct:.0f}%</b> downside)"
            lines.append(f"Target (Mean): <b>{fmt_money(tm)}</b>{ups}")
        hi_lo = []
        if fund.get("target_high") is not None:
            hi_lo.append(f"High {fmt_money(fund['target_high'])}")
        if fund.get("target_low") is not None:
            hi_lo.append(f"Low {fmt_money(fund['target_low'])}")
        if hi_lo:
            lines.append("  " + "  \u00b7  ".join(hi_lo))
        if fund.get("num_analysts"):
            lines.append(f"Analysts Covering: <b>{fund['num_analysts']}</b>")
        lines.append("")

    # Section 8: Shareholding QoQ trend
    lines.append("<b>\U0001F4BC SHAREHOLDING (QoQ TREND)</b>")
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
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
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_sym} NSE</i>")
    return lines
