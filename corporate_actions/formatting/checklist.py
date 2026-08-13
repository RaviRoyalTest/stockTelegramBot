"""32-point investment checklist: 10 personal + 22 AI criteria, scored per stock.

Pure formatting + evaluation - it takes the same quote/fund dicts every other
stock report uses (Yahoo quoteSummary + screener.in via sources.fundamentals)
and turns them into a pass/fail scorecard. Items that need data this bot
cannot fetch (5-year debt history, promoter pledge %, qualitative moat /
management judgement) are marked for manual review and are excluded from the
score, so the score only counts what was actually measured.

Rendering lives here (like formatting/stock.py); the command handler only
fetches data and calls format_checklist.
"""
from __future__ import annotations

from ..core.numbers import format_money
from ..core.text import escape

PASS, FAIL, NO_DATA = "pass", "fail", "nodata"

_STATUS_ICON = {PASS: "\u2705", FAIL: "\u274c", NO_DATA: "\u26aa"}


def _num(value, decimals: int = 2) -> str:
    """Number -> string with trailing zeros trimmed ('12.50' -> '12.5')."""
    try:
        formatted = f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _pct(value) -> str:
    """Fraction (0.15) -> percent string ('15.0%')."""
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _cr(value) -> str:
    """Rupee amount -> Crore string ('₹1,234.5Cr'), or 'N/A'."""
    try:
        return f"\u20b9{float(value) / 1e7:,.1f}Cr"
    except (TypeError, ValueError):
        return "N/A"


def _money(value, currency: str = "INR") -> str:
    """Currency-aware money string: ₹ crore for INR, compact $ for USD."""
    if currency == "USD":
        try:
            magnitude = abs(float(value))
            if magnitude >= 1e9:
                return f"${float(value) / 1e9:,.2f}B"
            if magnitude >= 1e6:
                return f"${float(value) / 1e6:,.1f}M"
            return f"${float(value):,.0f}"
        except (TypeError, ValueError):
            return "N/A"
    return _cr(value)


# --------------------------------------------------------------------------
# Item evaluators. Each returns (status, value_text) where status is one of
# PASS / FAIL / NO_DATA and value_text shows the measured numbers so the user
# can judge borderline cases themselves.
# --------------------------------------------------------------------------

def _debt_equity(fund):
    value = fund.get("debt_to_equity")
    if value is None:
        return NO_DATA, "no D/E data"
    status = PASS if float(value) < 1.0 else FAIL
    return status, f"D/E {_num(value, 2)}"


def _debt_trend(fund):
    # Only the current D/E snapshot is available - a 3-year trend needs
    # historical balance sheets, which the live feed does not provide.
    return NO_DATA, "needs 3-yr debt history"


def _revenue_growth(fund, threshold: float = 0.0):
    value = fund.get("revenue_growth")
    if value is None:
        return NO_DATA, "no revenue-growth data"
    status = PASS if float(value) > threshold else FAIL
    return status, f"Rev growth {_pct(value)}"


def _net_profit(fund):
    margin = fund.get("profit_margin")
    if margin is not None:
        status = PASS if float(margin) > 0 else FAIL
        return status, f"Net margin {_pct(margin)}"
    if fund.get("pe") is not None:
        # A trailing P/E exists only for profitable companies.
        return PASS, f"P/E {_num(fund['pe'], 1)} (profitable)"
    return NO_DATA, "no earnings data"


def _roe(fund, threshold: float):
    value = fund.get("roe")
    if value is None:
        return NO_DATA, "no ROE data (screener.in)"
    status = PASS if float(value) > threshold else FAIL
    return status, f"ROE {_num(value, 1)}%"


def _roce(fund, threshold: float = 15.0):
    value = fund.get("roce")
    if value is None:
        return NO_DATA, "no ROCE data (screener.in)"
    status = PASS if float(value) > threshold else FAIL
    return status, f"ROCE {_num(value, 1)}%"


def _earnings_growth(fund):
    value = fund.get("earnings_growth")
    if value is None:
        return NO_DATA, "no earnings-growth data"
    status = PASS if float(value) > 0 else FAIL
    return status, f"Earnings growth {_pct(value)}"


def _pe(fund, threshold: float = 10.0):
    value = fund.get("pe")
    if value is None:
        return NO_DATA, "no P/E (loss-making?)"
    status = PASS if float(value) < threshold else FAIL
    return status, f"P/E {_num(value, 1)}"


def _price_to_book(fund, threshold: float = 2.0):
    value = fund.get("price_to_book")
    if value is None:
        return NO_DATA, "no P/B data"
    status = PASS if float(value) < threshold else FAIL
    return status, f"P/B {_num(value, 2)}"


def _value_vs_sector(fund):
    """'Lower P/E and P/B is better' - scored against the sector average."""
    pe = fund.get("pe")
    sector_pe = fund.get("sector_pe")
    pb = fund.get("price_to_book")
    if pe is None or sector_pe is None:
        return NO_DATA, "needs stock + sector P/E"
    cheaper_pe = float(pe) <= float(sector_pe)
    cheap_pb = pb is None or float(pb) <= 2.0
    status = PASS if cheaper_pe and cheap_pb else FAIL
    return status, f"P/E {_num(pe, 1)} vs sector {_num(sector_pe, 1)}"


def _profit_margin(fund, threshold: float = 0.15):
    value = fund.get("profit_margin")
    if value is None:
        return NO_DATA, "no margin data"
    status = PASS if float(value) > threshold else FAIL
    return status, f"Net margin {_pct(value)}"


def _free_cashflow(fund):
    value = fund.get("free_cashflow")
    if value is None:
        return NO_DATA, "no FCF data"
    status = PASS if float(value) > 0 else FAIL
    return status, f"FCF {_money(value, fund.get('_currency', 'INR'))}"


def _interest_coverage(fund):
    # No interest-expense field in the Yahoo feed; needs the P&L statement.
    return NO_DATA, "no interest data (EBIT/Interest)"


def _current_ratio(fund, threshold: float = 1.5):
    value = fund.get("current_ratio")
    if value is None:
        return NO_DATA, "no current-ratio data"
    status = PASS if float(value) > threshold else FAIL
    return status, f"Current ratio {_num(value, 2)}"


def _pe_vs_history(fund):
    """P/E 'in line' proxy: cheaper than (or close to) the sector average."""
    pe = fund.get("pe")
    sector_pe = fund.get("sector_pe")
    if pe is None or sector_pe is None:
        return NO_DATA, "5-yr avg N/A - vs sector instead"
    status = PASS if float(pe) <= float(sector_pe) else FAIL
    return status, f"P/E {_num(pe, 1)} vs sector {_num(sector_pe, 1)}"


def _peg(fund, threshold: float = 1.5):
    pe = fund.get("pe")
    growth = fund.get("earnings_growth")
    if pe is None or growth is None or float(growth) <= 0:
        return NO_DATA, "needs P/E + positive growth"
    peg = float(pe) / (float(growth) * 100)
    status = PASS if peg < threshold else FAIL
    return status, f"PEG {_num(peg, 2)}"


def _price_to_fcf(fund, threshold: float = 20.0):
    currency = fund.get("_currency", "INR")
    if currency == "USD":
        mcap = fund.get("mcap_usd")
        fcf = fund.get("free_cashflow")
        if mcap is None or fcf is None or float(fcf) <= 0:
            return NO_DATA, "needs market cap + positive FCF"
        ratio = float(mcap) * 1e9 / float(fcf)
    else:
        mcap = fund.get("mcap_cr")
        fcf = fund.get("free_cashflow")
        if mcap is None or fcf is None or float(fcf) <= 0:
            return NO_DATA, "needs market cap + positive FCF"
        ratio = float(mcap) * 1e7 / float(fcf)
    status = PASS if ratio < threshold else FAIL
    return status, f"P/FCF {_num(ratio, 1)}"


def _ev_ebitda(fund, threshold: float = 15.0):
    ev = fund.get("enterprise_value")
    ebitda = fund.get("ebitda")
    if ev is None or ebitda is None or float(ebitda) <= 0:
        return NO_DATA, "needs EV + EBITDA"
    ratio = float(ev) / float(ebitda)
    status = PASS if ratio < threshold else FAIL
    return status, f"EV/EBITDA {_num(ratio, 1)}"


def _dividends(fund):
    value = fund.get("div_yield")
    if value is None:
        return NO_DATA, "no dividend data"
    status = PASS if float(value) > 0 else FAIL
    return status, f"Div yield {_num(value, 2)}%"


def _manual_review(reason: str):
    return NO_DATA, reason


# --------------------------------------------------------------------------
# The checklist itself. Personal = the trader's own 10 rules; AI = the 22
# institutional-quality criteria. Items that only a human can judge (moat,
# management, regulatory risk...) are marked manual-review and are not
# counted in the score - the score reflects measurable data only.
# --------------------------------------------------------------------------

PERSONAL_ITEMS = [
    ("Low debt-to-equity ratio (< 1.0)", _debt_equity),
    ("Decreasing debt over the past 3 years", _debt_trend),
    ("Increasing year-on-year sales", lambda f: _revenue_growth(f, 0.0)),
    ("Positive net profit in last 5 years", _net_profit),
    ("Return on Equity (ROE) > 20%", lambda f: _roe(f, 20.0)),
    ("Increasing net profit year-on-year", _earnings_growth),
    ("Consistent revenue growth (> 10% YoY)", lambda f: _revenue_growth(f, 0.10)),
    ("P/E < 10", lambda f: _pe(f, 10.0)),
    ("Low price-to-book (P/B < 2)", _price_to_book),
    ("P/E & P/B lower than sector (better value)", _value_vs_sector),
]

AI_ITEMS = [
    ("Strong profit margins (> 15%)", lambda f: _profit_margin(f, 0.15)),
    ("Positive Free Cash Flow (FCF)", _free_cashflow),
    ("ROE > 15% consistently", lambda f: _roe(f, 15.0)),
    ("ROCE/ROIC > 15%", _roce),
    ("Interest coverage > 4x-5x (EBIT / Interest)", _interest_coverage),
    ("Current ratio > 1.5 (liquidity)", lambda f: _current_ratio(f, 1.5)),
    ("Reasonable P/E (in-line with 5-yr average)", _pe_vs_history),
    ("PEG ratio < 1.5 (not overpaying for growth)", _peg),
    ("Price-to-Free-Cash-Flow < 20", _price_to_fcf),
    ("EV/EBITDA reasonable vs peers", _ev_ebitda),
    ("Consistent dividends or steady buybacks", _dividends),
    ("Strong economic moat (brand / network / switching cost)",
     lambda f: _manual_review("manual review")),
    ("Can raise prices during inflation without losing customers",
     lambda f: _manual_review("manual review")),
    ("Transparent, capable & shareholder-friendly management",
     lambda f: _manual_review("manual review")),
    ("No single customer accounts for > 10% of revenue",
     lambda f: _manual_review("manual review")),
    ("Adequate R&D investment to stay ahead of competition",
     lambda f: _manual_review("manual review")),
    ("Clear growth strategy & addressable market",
     lambda f: _manual_review("manual review")),
    ("No frequent auditor changes / accounting red flags",
     lambda f: _manual_review("manual review")),
    ("Promoter pledged shares < 5% (ideally zero)",
     lambda f: _manual_review("manual review")),
    ("Outstanding shares stable - no excessive dilution",
     lambda f: _manual_review("manual review")),
    ("Not overly vulnerable to sudden regulatory changes",
     lambda f: _manual_review("manual review")),
    ("Business can survive recession / high-rate cycles",
     lambda f: _manual_review("manual review")),
]


def _score_items(items, fund) -> tuple[list, int, int]:
    """Evaluate one section; returns (rows, passed, checked)."""
    rows = []
    passed = checked = 0
    for label, evaluator in items:
        status, value = evaluator(fund)
        if status != NO_DATA:
            checked += 1
            passed += status == PASS
        rows.append((label, status, value))
    return rows, passed, checked


def _verdict(passed: int, checked: int) -> str:
    """One-line take on the score, based only on the measured items."""
    if checked == 0:
        return "\u26aa <b>Not enough data to score</b> - sources may be down."
    ratio = passed / checked
    if ratio >= 0.8:
        return f"\U0001F7E2 <b>Strong candidate</b> - {ratio * 100:.0f}% of checked items passed."
    if ratio >= 0.6:
        return (f"\U0001F7E1 <b>Decent</b> - {ratio * 100:.0f}% of checked items passed. "
                "Review the \u26aa manual items before deciding.")
    return (f"\U0001F534 <b>Weak</b> - only {ratio * 100:.0f}% of checked items passed. "
            "Avoid or deep-dive with /fundamentalreport.")


def _section_block(title: str, total: int, rows, passed: int, checked: int) -> list[str]:
    lines = [f"<b>{title} ({passed}/{total})</b>"]
    for label, status, value in rows:
        icon = _STATUS_ICON[status]
        if status == NO_DATA:
            lines.append(f"{icon} {escape(label)} \u2014 <i>{escape(value)}</i>")
        else:
            lines.append(f"{icon} {escape(label)} \u2014 {escape(value)}")
    lines.append("")
    return lines


def format_checklist(raw_symbol: str, quote: dict, fund: dict | None,
                     currency: str = "INR") -> list[str]:
    """Build the full checklist report lines for one symbol (Telegram HTML)."""
    fund = dict(fund or {})
    fund["_currency"] = "USD" if (currency or "INR").upper() == "USD" else "INR"
    price = quote.get("price")
    company_name = quote.get("name") or raw_symbol

    personal_rows, personal_pass, personal_checked = _score_items(PERSONAL_ITEMS, fund)
    ai_rows, ai_pass, ai_checked = _score_items(AI_ITEMS, fund)

    total_pass = personal_pass + ai_pass
    total_checked = personal_checked + ai_checked
    manual = len(PERSONAL_ITEMS) + len(AI_ITEMS) - total_checked

    lines = [
        f"\U0001F4DD <b>INVESTMENT CHECKLIST</b> \u2014 <b>{escape(company_name.upper())}</b> "
        f"(<code>{escape(raw_symbol)}</code>)",
    ]
    if fund.get("sector"):
        lines.append(f"Sector: <i>{escape(fund['sector'])}</i>")
    if price is not None:
        lines.append(f"Current Price: <b>{format_money(price, fund['_currency'])}</b>")
    lines.append("")

    lines.extend(_section_block(
        "\U0001F464 PERSONAL CRITERIA", len(PERSONAL_ITEMS),
        personal_rows, personal_pass, personal_checked,
    ))
    lines.extend(_section_block(
        "\U0001F916 AI CRITERIA", len(AI_ITEMS),
        ai_rows, ai_pass, ai_checked,
    ))

    lines.append(
        f"<b>TOTAL: {total_pass}/{len(PERSONAL_ITEMS) + len(AI_ITEMS)} \u2705</b> "
        f"(checked {total_checked}, {manual} manual)"
    )
    lines.append(_verdict(total_pass, total_checked))
    lines.append("")
    lines.append(
        "\u26aa items need manual review - the live feed cannot measure them. "
        "Scores only count the items the bot could check."
    )
    lines.append(f"\U0001F4A1 <i>Deep-dive: /fundamentalreport {raw_symbol}</i>")
    return lines
