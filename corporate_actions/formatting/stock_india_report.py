"""Indian DEEP fundamental report renderer (/fundamentalreport, /fund).

Pure formatting: builds the long multi-section report (valuation, growth &
margins, per-share & scale, balance sheet & cash flow, returns, screener.in
annuals/quarters tables, analyst view, shareholding, snapshot, verdict). No
fetching here - the quote/fund dicts are passed in. The quick card lives in
stock_india_card.py, movers rows in stock_india_movers.py, shared helpers in
stock_common.py.
"""
from __future__ import annotations

import re

from ..core.numbers import format_money
from ..core.text import escape
from .stock_common import (
    _consensus_label,
    _num_or_na,
    _pct_str,
    _RATING_LABELS,
)

_DIVIDER = "\u2501" * 20

_GREEN = "\U0001F7E2"
_YELLOW = "\U0001F7E1"
_RED = "\U0001F534"
_DOWN = "\U0001F53B"


def _short_year(label) -> str:
    """'Mar 2022' / 'Jun 2026' -> 'Mar'22' / 'Jun'26'."""
    match = re.search(r"([A-Za-z]+)\s*(\d{4})", label or "")
    if match:
        return f"{match.group(1)}'{match.group(2)[2:]}"
    return label or ""


def _fy_label(label) -> str:
    """'Mar 2021' / 'FY21' -> 'FY21' (screener fiscal-year label)."""
    match = re.search(r"(\d{4})", label or "")
    if match:
        return f"FY{match.group(1)[2:]}"
    match = re.search(r"FY\s*(\d+)", label or "")
    if match:
        return f"FY{match.group(1)}"
    return label or ""


def _cr_cr(value) -> str:
    """screener.in money in ₹ Crore (the source already reports Cr units)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "-" if number < 0 else ""
    return f"{sign}\u20b9{_inr_group(abs(number))} Cr"


def _inr_group(value, decimals: int = 0) -> str:
    """Format with Indian digit grouping (1,05,647) and fixed decimals."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "-" if number < 0 else ""
    number = abs(number)
    text = f"{number:.{decimals}f}"
    if "." in text:
        int_part, frac_part = text.split(".")
    else:
        int_part, frac_part = text, ""
    if len(int_part) <= 3:
        grouped = int_part
    else:
        head, tail = int_part[:-3], int_part[-3:]
        parts = [tail]
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts)
    if frac_part:
        return f"{sign}{grouped}.{frac_part}"
    return f"{sign}{grouped}"


def _num_1dp(value) -> str:
    """Number with exactly one decimal ('8.0'), or 'N/A'."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "N/A"


def _arrow_pct(percent) -> str:
    """Percent (already in % units) with a green/red indicator."""
    if percent is None:
        return "N/A"
    icon = _GREEN if percent >= 0 else _DOWN
    return f"{icon} {percent:+.1f}%"


def _position_label(price, fund: dict) -> str | None:
    """52-week position label, e.g. '🟢 At High' / '🔴 At Low' / '🟡 Mid-Range'."""
    low = fund.get("wk52_low")
    high = fund.get("wk52_high")
    if low is None or high is None or price is None:
        return None
    try:
        price = float(price)
        low = float(low)
        high = float(high)
    except (TypeError, ValueError):
        return None
    spread = high - low
    if spread <= 0:
        return None
    percent_position = (price - low) / spread
    if percent_position >= 0.95:
        return f"{_GREEN} At High"
    if percent_position >= 0.75:
        return f"{_GREEN} Near High"
    if percent_position <= 0.05:
        return f"{_RED} At Low"
    if percent_position <= 0.25:
        return f"{_RED} Near Low"
    return f"{_YELLOW} Mid-Range"


def _section(emoji: str, title: str) -> list[str]:
    """A section header + divider line."""
    return [f"{emoji} <b>{title}</b>", _DIVIDER]


def _pe_signal(pe) -> str:
    """Trailing/forward P/E traffic light."""
    try:
        value = float(pe)
    except (TypeError, ValueError):
        return ""
    if value > 40:
        return f" {_RED}"
    if value >= 20:
        return f" {_YELLOW}"
    return f" {_GREEN}"


def _de_signal(de) -> str:
    """Debt-to-equity traffic light."""
    try:
        value = float(de)
    except (TypeError, ValueError):
        return ""
    if value <= 0.5:
        return f" {_GREEN}"
    if value <= 1.0:
        return f" {_YELLOW}"
    return f" {_RED}"


def _coverage_signal(value) -> str:
    """Interest-coverage traffic light."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number >= 3:
        return f" {_GREEN}"
    if number >= 1.5:
        return f" {_YELLOW}"
    return f" {_RED}"


def _return_signal(value) -> str:
    """ROCE/ROE traffic light."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number >= 15:
        return f" {_GREEN}"
    if number >= 10:
        return f" {_YELLOW}"
    return f" {_RED}"


def _rsi_label(rsi) -> str:
    """RSI value + traffic light + zone label, e.g. '43.6 🟢 Low'."""
    if rsi is None:
        return "N/A"
    try:
        value = float(rsi)
    except (TypeError, ValueError):
        return "N/A"
    if value <= 30:
        zone, icon = "Oversold", _GREEN
    elif value < 45:
        zone, icon = "Low", _GREEN
    elif value >= 70:
        zone, icon = "Overbought", _RED
    elif value >= 60:
        zone, icon = "High", _RED
    else:
        zone, icon = "Mid", _YELLOW
    return f"{_num_or_na(value, 1)} {icon} {zone}"


def _macd_crossover(line, signal) -> str | None:
    """'🟢 Bullish Crossover' / '🔴 Bearish Crossover', or None."""
    if line is None or signal is None:
        return None
    bull = line >= signal
    return f"{_GREEN if bull else _RED} {'Bullish' if bull else 'Bearish'} Crossover"


def _holding_str(value) -> str:
    """'38.09% (🟢 +0.48%)' -> '38.09% 🟢 +0.48%'."""
    match = re.match(r"([\d.]+%)\s*\(([^)]+)\)", value or "")
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value or ""


def _holding_delta(value):
    """Signed QoQ delta from a shareholding string ('(🟢 +0.48%)' -> 0.48)."""
    match = re.search(r"([+-]?\d+(?:\.\d+)?)%\)", value or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _holding_trend(value):
    """'🟢 Positive' / '🔴 Negative' / None from a shareholding delta."""
    delta = _holding_delta(value)
    if delta is None:
        return None
    return (_GREEN, "Positive") if delta > 0 else (_RED, "Negative")


def _cmp_line(quote: dict) -> str | None:
    """'CMP: ₹7,222.00 🟢 +0.66%'."""
    price = quote.get("price")
    if price is None:
        return None
    price_str = format_money(price)
    change_pct = quote.get("change_pct")
    if change_pct is not None:
        icon = _GREEN if change_pct >= 0 else _RED
        sign = "+" if change_pct >= 0 else ""
        return f"CMP: <b>{price_str}</b> {icon} {sign}{change_pct:.2f}%"
    return f"CMP: <b>{price_str}</b>"


def _technical_lines(fund: dict, price) -> list[str]:
    """RSI / MACD / SMA 50-200 detail block."""
    rsi = fund.get("rsi")
    line, signal, hist = fund.get("macd_line"), fund.get("macd_signal"), fund.get("macd_hist")
    sma_50, sma_200 = fund.get("sma_50"), fund.get("sma_200")
    if (rsi is None and line is None and hist is None
            and sma_50 is None and sma_200 is None):
        return []
    out = _section("\U0001F4C8", "TECHNICAL INDICATORS")
    if rsi is not None:
        out.append(f"RSI(14): <b>{_rsi_label(rsi)}</b>")
    if line is not None:
        out.append(f"MACD: <b>{_num_or_na(line, 2)}</b>")
    if signal is not None:
        out.append(f"Signal: <b>{_num_or_na(signal, 2)}</b>")
    if hist is not None:
        out.append(f"Histogram: <b>{_num_or_na(hist, 2)}</b>")
    crossover = _macd_crossover(line, signal)
    if crossover:
        out.append(f"MACD Signal: <b>{crossover}</b>")
    if sma_50 is not None or sma_200 is not None:
        if sma_50 is not None:
            out.append(f"SMA 50D: <b>{format_money(sma_50)}</b>")
        if sma_200 is not None:
            out.append(f"SMA 200D: <b>{format_money(sma_200)}</b>")
        if price:
            for name, sma in (("50D", sma_50), ("200D", sma_200)):
                if sma is not None:
                    try:
                        above = float(price) >= float(sma)
                    except (TypeError, ValueError):
                        continue
                    icon = _GREEN if above else _RED
                    out.append(f"Price vs {name}: <b>{icon} {'Above' if above else 'Below'}</b>")
    return out


def _valuation_lines(fund: dict) -> list[str]:
    out = _section("\U0001F3F7\ufe0f", "VALUATION")
    if fund.get("pe"):
        out.append(f"P/E: <b>{_num_or_na(fund['pe'], 1)}x</b>{_pe_signal(fund['pe'])}")
    else:
        out.append("P/E: <b>N/A (Loss)</b>")
    if fund.get("forward_pe"):
        out.append(f"Forward P/E: <b>{_num_or_na(fund['forward_pe'], 1)}x</b>{_pe_signal(fund['forward_pe'])}")
    if fund.get("sector_pe"):
        out.append(f"Sector P/E: <b>{_num_or_na(fund['sector_pe'], 1)}x</b>")
    if fund.get("price_to_book"):
        out.append(f"P/B: <b>{_num_or_na(fund['price_to_book'], 2)}x</b>")
    if fund.get("price_to_sales"):
        out.append(f"P/S: <b>{_num_or_na(fund['price_to_sales'], 2)}x</b>")
    if fund.get("div_yield") is not None:
        out.append(f"Dividend Yield: <b>{_num_or_na(fund['div_yield'], 2)}%</b>")
    return out


def _growth_margin_lines(fund: dict) -> list[str]:
    growth = []
    if fund.get("earnings_growth") is not None:
        growth.append(f"Earnings YoY: {_arrow_pct(fund['earnings_growth'])}")
    if fund.get("revenue_growth") is not None:
        growth.append(f"Revenue YoY: {_arrow_pct(fund['revenue_growth'])}")
    margins = []
    if fund.get("gross_margin") is not None:
        margins.append(f"Gross Profit: {_pct_str(fund['gross_margin'])}")
    if fund.get("ebitda_margin") is not None:
        margins.append(f"EBITDA: {_pct_str(fund['ebitda_margin'])}")
    if fund.get("operating_margin") is not None:
        margins.append(f"Operating Profit: {_pct_str(fund['operating_margin'])}")
    if fund.get("profit_margin") is not None:
        margins.append(f"Net Profit: {_pct_str(fund['profit_margin'])}")
    if not growth and not margins:
        return []
    out = _section("\U0001F4C8", "GROWTH & MARGINS")
    out.extend(growth)
    out.extend(margins)
    return out


def _per_share_lines(fund: dict) -> list[str]:
    out = _section("\U0001F4BC", "PER-SHARE & SCALE")
    if fund.get("trailing_eps") is not None:
        out.append(f"EPS (TTM): <b>{format_money(fund['trailing_eps'])}</b>")
    if fund.get("forward_eps") is not None:
        out.append(f"EPS (Forward): <b>{format_money(fund['forward_eps'])}</b>")
    if fund.get("revenue_per_share") is not None:
        out.append(f"Revenue/Share: <b>{format_money(fund['revenue_per_share'])}</b>")
    if fund.get("book_value") is not None:
        out.append(f"Book Value: <b>{format_money(fund['book_value'])}</b>")
    if fund.get("shares_outstanding") is not None:
        out.append(f"Shares Outstanding: <b>{fund['shares_outstanding'] / 1e7:,.2f} Cr</b>")
    return out


def _balance_sheet_lines(fund: dict) -> list[str]:
    bs = fund.get("balance_sheet") or {}
    out = _section("\U0001F3E6", "BALANCE SHEET")
    if fund.get("debt_to_equity") is not None:
        out.append(f"Debt/Equity: <b>{_num_or_na(fund['debt_to_equity'], 2)}x</b>{_de_signal(fund['debt_to_equity'])}")
    if fund.get("interest_coverage_ratio") is not None:
        out.append(f"Interest Coverage: <b>{_num_or_na(fund['interest_coverage_ratio'], 1)}x</b>{_coverage_signal(fund['interest_coverage_ratio'])}")
    if bs.get("net_worth") is not None:
        out.append(f"Net Worth: <b>{_cr_cr(bs['net_worth'])}</b>")
    if bs.get("borrowings") is not None:
        out.append(f"Borrowings: <b>{_cr_cr(bs['borrowings'])}</b>")
    if bs.get("total_assets") is not None:
        out.append(f"Total Assets: <b>{_cr_cr(bs['total_assets'])}</b>")
    return out


def _cash_flow_lines(fund: dict) -> list[str]:
    cf = fund.get("cash_flow") or {}
    items = [
        ("CFO", cf.get("cfo"), True),
        ("CFI", cf.get("cfi"), False),
        ("CFF", cf.get("cff"), False),
        ("Net Cash Flow", cf.get("net_cash_flow"), True),
        ("Free Cash Flow", cf.get("free_cash_flow"), True),
    ]
    if not any(value is not None for _, value, _ in items):
        return []
    year = _fy_label(cf.get("year")) or ""
    title = "CASH FLOW" + (f" \u2014 {year}" if year else "")
    out = _section("\U0001F4B5", title)
    for label, value, flag in items:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        sign = "+" if number > 0 else ("-" if number < 0 else "")
        icon = f" {_RED}" if flag and number < 0 else ""
        out.append(f"{label}: <b>{sign}\u20b9{_inr_group(abs(number))} Cr</b>{icon}")
    return out


def _returns_lines(fund: dict) -> list[str]:
    out = _section("\U0001F3AF", "RETURNS")
    if fund.get("roce") is not None:
        out.append(f"ROCE: <b>{_num_1dp(fund['roce'])}%</b>{_return_signal(fund['roce'])}")
    if fund.get("roe") is not None:
        out.append(f"ROE: <b>{_num_1dp(fund['roe'])}%</b>{_return_signal(fund['roe'])}")
    return out


def _annual_trend_lines(fund: dict) -> list[str]:
    """Last-6 fiscal-year P&L blocks (screener.in annuals)."""
    annuals = fund.get("annuals") or []
    if not annuals:
        return []
    recent = annuals[-6:]
    lines = []
    for index, item in enumerate(recent):
        is_latest = index == len(recent) - 1
        lines.append(_fy_label(item.get("year")) + ":")
        sales = f"{item['sales']:,.0f}" if item.get("sales") is not None else "N/A"
        s_growth = item.get("sales_growth")
        sg = "N/A" if s_growth is None else f"{s_growth:+.1f}%"
        if is_latest and s_growth is not None:
            sg += f" {_GREEN if s_growth >= 0 else _DOWN}"
        lines.append(f"Sales: \u20b9{sales} Cr | YoY: {sg}")
        opm = f"{item['opm']:.0f}" if item.get("opm") is not None else "N/A"
        net = f"{item['net_profit']:,.0f}" if item.get("net_profit") is not None else "N/A"
        lines.append(f"OPM: {opm}% | Profit: \u20b9{net} Cr")
        n_growth = item.get("profit_growth")
        ng = "N/A" if n_growth is None else f"{n_growth:+.1f}%"
        eps = f"{item['eps']:.1f}" if item.get("eps") is not None else "N/A"
        if is_latest and n_growth is not None:
            ng += f" {_GREEN if n_growth >= 0 else _DOWN}"
        lines.append(f"NP YoY: {ng} | EPS: {eps}")
        roce = f"{item['roce']:.0f}" if item.get("roce") is not None else "N/A"
        lines.append(f"ROCE: {roce}%")
        if not is_latest:
            lines.append("")
    return lines


def _quarterly_lines(fund: dict) -> list[str]:
    """Last-6-quarter Sales/OPM/Net Profit/EPS blocks + latest stats."""
    quarters = fund.get("quarters") or []
    if not quarters:
        return []
    recent = quarters[-6:]
    lines = []
    for index, item in enumerate(recent):
        lines.append(_short_year(item.get("quarter")) + ":")
        sales = f"{item['sales']:,.0f}" if item.get("sales") is not None else "N/A"
        opm = f"{item['opm']:.0f}" if item.get("opm") is not None else "N/A"
        lines.append(f"Sales: \u20b9{sales} Cr | OPM: {opm}%")
        net = item.get("net_profit")
        net_str = "N/A" if net is None else f"{'-' if net < 0 else ''}\u20b9{abs(net):,.0f} Cr"
        eps = item.get("eps")
        eps_str = "N/A" if eps is None else f"{eps:.1f}"
        lines.append(f"Profit: {net_str} | EPS: {eps_str}")
        if index < len(recent) - 1:
            lines.append("")
    latest, previous = recent[-1], recent[-2] if len(recent) >= 2 else None
    bits = []
    if (
        previous
        and latest.get("net_profit") is not None
        and previous.get("net_profit") is not None
        and previous["net_profit"] != 0
    ):
        qoq = (latest["net_profit"] - previous["net_profit"]) / abs(previous["net_profit"]) * 100
        bits.append(f"Latest QoQ Profit: {_arrow_pct(qoq)}")
    if latest.get("opm") is not None:
        bits.append(f"Latest OPM: {latest['opm']:.0f}%")
    if latest.get("eps") is not None:
        bits.append(f"Latest EPS: \u20b9{_num_or_na(latest['eps'], 2)}")
    if bits:
        lines.append("")
        lines.extend(bits)
    return lines


def _analyst_view_lines(fund: dict, price) -> list[str]:
    if not (fund.get("num_analysts") or fund.get("target_mean")
            or fund.get("rec_mean") or fund.get("rec_trend")):
        return []
    out = _section("\U0001F52D", "ANALYST VIEW")
    consensus = _consensus_label(fund)
    if consensus:
        icon = {"Strong Buy": _GREEN, "Buy": _GREEN, "Hold": _YELLOW,
                "Sell": _RED, "Strong Sell": _RED}.get(consensus, _YELLOW)
        out.append(f"Consensus: {icon} <b>{consensus.upper()}</b>")
    if fund.get("num_analysts"):
        out.append(f"Analysts Covering: <b>{fund['num_analysts']}</b>")
    trend = fund.get("rec_trend") or {}
    if any(trend.values()):
        line1 = " | ".join(f"{label}: {trend.get(key, 0)}" for key, label in _RATING_LABELS[:3])
        line2 = " | ".join(f"{label}: {trend.get(key, 0)}" for key, label in _RATING_LABELS[3:])
        out.append(line1)
        out.append(line2)
    if fund.get("target_mean") is not None:
        out.append(f"Mean Target: <b>\u20b9{_inr_group(fund['target_mean'], 2)}</b>")
        if price:
            try:
                upside = (float(fund["target_mean"]) - float(price)) / float(price) * 100
            except (TypeError, ValueError):
                upside = None
            if upside is not None:
                icon = _GREEN if upside >= 0 else _RED
                out.append(f"Potential Upside: {icon} {upside:+.0f}%")
    targets = []
    if fund.get("target_high") is not None:
        targets.append(f"High Target: <b>\u20b9{_inr_group(fund['target_high'])}</b>")
    if fund.get("target_median") is not None:
        targets.append(f"Median Target: <b>\u20b9{_inr_group(fund['target_median'], 2)}</b>")
    if fund.get("target_low") is not None:
        targets.append(f"Low Target: <b>\u20b9{_inr_group(fund['target_low'])}</b>")
    if targets:
        out.extend(targets)
    return out


def _rating_trend_lines(fund: dict) -> list[str]:
    """Rating-breakdown history (now, 1M, 2M, 3M ago) with trend arrows."""
    history = fund.get("rec_history") or []
    if len(history) < 2:
        return []
    out = _section("\U0001F4C5", "RATING TREND")
    labels = dict(_RATING_LABELS)
    for index, row in enumerate(history):
        period = (row.get("period") or "").strip()
        if period in ("", "0m", "0"):
            when = "Now"
        else:
            when = f"{period.lstrip('-').rstrip('m')}M Ago"
        line1 = " | ".join(f"{labels[key]}: {row.get(key, 0)}" for key, _ in _RATING_LABELS[:3])
        line2 = " | ".join(f"{labels[key]}: {row.get(key, 0)}" for key, _ in _RATING_LABELS[3:])
        arrow = ""
        if index > 0:
            previous = history[index - 1]
            improved = any(
                row.get(key, 0) > previous.get(key, 0)
                for key in ("strong_buy", "buy", "hold")
            ) or row.get("sell", 0) < previous.get("sell", 0)
            worsened = any(
                row.get(key, 0) < previous.get(key, 0)
                for key in ("strong_buy", "buy", "hold")
            ) or row.get("sell", 0) > previous.get("sell", 0)
            if improved and not worsened:
                arrow = f" {_GREEN}"
            elif worsened and not improved:
                arrow = f" {_DOWN}"
        out.append(f"{when}:")
        out.append(line1)
        out.append(line2 + arrow)
        if index < len(history) - 1:
            out.append("")
    return out


def _shareholding_lines(fund: dict) -> list[str]:
    if not any(fund.get(key) for key in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        return [_section("\U0001F465", "SHAREHOLDING")[0], "No shareholding breakdown available."]
    out = _section("\U0001F465", "SHAREHOLDING")
    for key, label in (
        ("promoter_pct", "Promoter"),
        ("fii_pct", "FII"),
        ("dii_pct", "DII"),
        ("public_pct", "Public"),
    ):
        value = fund.get(key)
        if value:
            out.append(f"{label}: {escape(_holding_str(value))}")
    return out


def _management_lines(fund: dict) -> list[str]:
    officers = fund.get("officers") or []
    if not officers:
        return []
    out = _section("\U0001F464", "TOP MANAGEMENT")
    for officer in officers[:5]:
        name = (officer.get("name") or "").strip()
        title = (officer.get("title") or "").strip()
        if name:
            out.append(f"\u2022 <b>{escape(name)}</b> \u2014 {escape(title or 'Director')}")
    return out


def _technicals_rating(fund: dict, price):
    """Overall technicals rating: (icon, word) / None."""
    sma_50, sma_200 = fund.get("sma_50"), fund.get("sma_200")
    below, total = 0, 0
    if price:
        for sma in (sma_50, sma_200):
            if sma is not None:
                total += 1
                try:
                    if float(price) < float(sma):
                        below += 1
                except (TypeError, ValueError):
                    total -= 1
    macd_bear = (fund.get("macd_line") is not None and fund.get("macd_signal") is not None
                 and fund["macd_line"] < fund["macd_signal"])
    if macd_bear:
        total += 1
    if total == 0:
        return None
    if macd_bear or (total > 0 and below == total):
        return (_RED, "Weak")
    if below == 0 and not macd_bear:
        return (_GREEN, "Strong")
    return (_YELLOW, "Mixed")


def _overall_ratings(fund: dict, price) -> dict:
    """Per-dimension ratings for the snapshot + overall view + verdict."""
    ratings = {}

    rev = fund.get("revenue_growth")
    if rev is None:
        ratings["Business Growth"] = None
    elif rev >= 10:
        ratings["Business Growth"] = (_GREEN, "Good")
    elif rev > 0:
        ratings["Business Growth"] = (_YELLOW, "Moderate")
    else:
        ratings["Business Growth"] = (_RED, "Weak")

    roe, roce = fund.get("roe"), fund.get("roce")
    margin = fund.get("profit_margin")
    if roe is None and roce is None and margin is None:
        ratings["Profitability"] = None
    elif (roe is not None and roe >= 15) or (roce is not None and roce >= 15) or (
        margin is not None and margin >= 0.15
    ):
        ratings["Profitability"] = (_GREEN, "Good")
    elif (roe is None or roe < 10) and (roce is None or roce < 10) and (
        margin is not None and margin < 0.08
    ):
        ratings["Profitability"] = (_RED, "Weak")
    else:
        ratings["Profitability"] = (_YELLOW, "Moderate")

    de = fund.get("debt_to_equity")
    if de is None:
        ratings["Balance Sheet"] = None
    elif de <= 0.5:
        ratings["Balance Sheet"] = (_GREEN, "Reasonable")
    elif de <= 1.0:
        ratings["Balance Sheet"] = (_YELLOW, "Moderate")
    else:
        ratings["Balance Sheet"] = (_RED, "Stretched")

    cf = fund.get("cash_flow") or {}
    fcf = cf.get("free_cash_flow")
    if fcf is None:
        ratings["Cash Flow"] = None
    elif fcf >= 0:
        ratings["Cash Flow"] = (_GREEN, "Strong")
    else:
        ratings["Cash Flow"] = (_RED, "Weak")

    pe = fund.get("pe")
    if pe is None:
        ratings["Valuation"] = None
    elif float(pe) > 40:
        ratings["Valuation"] = (_RED, "Expensive")
    elif float(pe) >= 20:
        ratings["Valuation"] = (_YELLOW, "Moderate")
    else:
        ratings["Valuation"] = (_GREEN, "Cheap")

    ratings["Technicals"] = _technicals_rating(fund, price)

    consensus = _consensus_label(fund)
    if consensus in ("Strong Buy", "Buy"):
        ratings["Analyst View"] = (_GREEN, "Positive")
    elif consensus in ("Sell", "Strong Sell"):
        ratings["Analyst View"] = (_RED, "Negative")
    elif consensus:
        ratings["Analyst View"] = (_YELLOW, "Neutral")
    else:
        ratings["Analyst View"] = None
    return ratings


def _snapshot_lines(fund: dict, price) -> list[str]:
    ratings = _overall_ratings(fund, price)

    def _return_rating(value):
        if value is None:
            return None
        if value >= 15:
            return (_GREEN, "Strong")
        if value >= 10:
            return (_YELLOW, "Moderate")
        return (_RED, "Weak")

    annuals = fund.get("annuals") or []
    pg = annuals[-1].get("profit_growth") if annuals else None
    if pg is None:
        pg = fund.get("earnings_growth")
    profit_rating = (_GREEN, "Positive") if pg is not None and pg > 0 else (
        (_RED, "Negative") if pg is not None else None)

    de = fund.get("debt_to_equity")
    if de is None:
        debt_rating = None
    elif de <= 0.5:
        debt_rating = (_GREEN, "Moderate")
    elif de <= 1.0:
        debt_rating = (_YELLOW, "Moderate")
    else:
        debt_rating = (_RED, "High")

    items = [
        ("Revenue Growth", ratings.get("Business Growth")),
        ("Profit Growth", profit_rating),
        ("Debt", debt_rating),
        ("ROCE", _return_rating(fund.get("roce"))),
        ("ROE", _return_rating(fund.get("roe"))),
        ("Cash Flow", ratings.get("Cash Flow")),
        ("Valuation", ratings.get("Valuation")),
        ("Technicals", ratings.get("Technicals")),
        ("Analyst Sentiment", ratings.get("Analyst View")),
        ("DII Trend", _holding_trend(fund.get("dii_pct"))),
        ("FII Trend", _holding_trend(fund.get("fii_pct"))),
    ]
    out = _section("\U0001F9E0", "FUNDAMENTAL SNAPSHOT")
    for label, rating in items:
        if rating:
            icon, word = rating
            out.append(f"{label}: {icon} {word}")
    return out


def _verdict(ratings: dict) -> str:
    green = sum(1 for rating in ratings.values() if rating and rating[0] == _GREEN)
    red = sum(1 for rating in ratings.values() if rating and rating[0] == _RED)
    if green >= 5 and red <= 1:
        return f"{_GREEN} BUY"
    if red >= 5 and green <= 1:
        return f"{_RED} SELL / AVOID"
    return f"{_YELLOW} WATCH / WAIT"


def _main_question(fund: dict, price) -> list[str]:
    """A one-line question reflecting the report's key tension."""
    pe = fund.get("pe")
    annuals = fund.get("annuals") or []
    latest_growth = annuals[-1].get("profit_growth") if annuals else None
    if pe is not None and float(pe) > 40 and latest_growth is not None and latest_growth >= 0:
        return ["Can future earnings growth justify the", "current high valuation?"]
    if fund.get("roe") is not None and fund["roe"] < 10:
        return ["Can profitability improve from the", "current low ROE levels?"]
    cf = fund.get("cash_flow") or {}
    if cf.get("free_cash_flow") is not None and cf["free_cash_flow"] < 0:
        return ["Can the company turn around its", "negative cash flow?"]
    return ["Is the current growth rate sustainable", "over the next few years?"]


def _fund_report_lines(raw_symbol, quote, fund, include_tip=True, label="") -> list[str]:
    """Build the deep /fund fundamental report for one symbol."""
    quote = quote or {}
    fund = fund or {}
    price = quote.get("price")
    company_name = quote.get("name") or raw_symbol

    lines = []
    label_prefix = f"{label} " if label else ""

    # Title + company identity
    lines.append(_DIVIDER)
    lines.append("\U0001F4CA <b>FUNDAMENTAL REPORT</b>")
    lines.append(_DIVIDER)
    lines.append(f"{label_prefix}<b>{escape(company_name.upper())}</b>")

    # Section 1: Price & movement
    position_label = _position_label(price, fund)
    if price is not None or (
        fund.get("wk52_high") is not None and fund.get("wk52_low") is not None
    ) or position_label:
        lines.extend(_section("\U0001F4B0", "PRICE & MOVEMENT"))
        cmp_line = _cmp_line(quote)
        if cmp_line:
            lines.append(cmp_line)
        if fund.get("wk52_low") is not None:
            lines.append(f"52W Low: <b>{format_money(fund['wk52_low'])}</b>")
        if fund.get("wk52_high") is not None:
            lines.append(f"52W High: <b>{format_money(fund['wk52_high'])}</b>")
        if position_label:
            lines.append(f"Technical Position: <b>{position_label}</b>")
        mcap = fund.get("market_cap")
        if mcap is None:
            mcap = fund.get("mcap_cr")
        if mcap is not None:
            lines.append(f"Market Cap: <b>\u20b9{_inr_group(mcap)} Cr</b>")
        lines.append("")

    # Technical indicators
    tech_lines = _technical_lines(fund, price)
    if tech_lines:
        lines.extend(tech_lines)
        lines.append("")

    # Valuation
    lines.extend(_valuation_lines(fund))
    lines.append("")

    # Growth & margins
    growth_lines = _growth_margin_lines(fund)
    if growth_lines:
        lines.extend(growth_lines)
        lines.append("")

    # Per-share & scale
    lines.extend(_per_share_lines(fund))
    lines.append("")

    # Balance sheet
    lines.extend(_balance_sheet_lines(fund))
    lines.append("")

    # Cash flow
    cf_lines = _cash_flow_lines(fund)
    if cf_lines:
        lines.extend(cf_lines)
        lines.append("")

    # Returns
    lines.extend(_returns_lines(fund))
    lines.append("")

    # Annual P&L trend (5-year)
    annual_lines = _annual_trend_lines(fund)
    if annual_lines:
        lines.extend(_section("\U0001F4C8", "5-YEAR PERFORMANCE"))
        lines.extend(annual_lines)
        lines.append("")

    # Quarterly results
    quarter_lines = _quarterly_lines(fund)
    if quarter_lines:
        lines.extend(_section("\U0001F4C5", "QUARTERLY RESULTS"))
        lines.extend(quarter_lines)
        lines.append("")

    # Analyst view
    analyst_lines = _analyst_view_lines(fund, price)
    if analyst_lines:
        lines.extend(analyst_lines)
        lines.append("")

    # Rating trend
    trend_lines = _rating_trend_lines(fund)
    if trend_lines:
        lines.extend(trend_lines)
        lines.append("")

    # Shareholding
    lines.extend(_shareholding_lines(fund))
    lines.append("")

    # Top management
    mgmt_lines = _management_lines(fund)
    if mgmt_lines:
        lines.extend(mgmt_lines)
        lines.append("")

    # Fundamental snapshot
    snapshot_lines = _snapshot_lines(fund, price)
    if snapshot_lines:
        lines.extend(snapshot_lines)
        lines.append("")

    # Key concerns
    ratings = _overall_ratings(fund, price)
    concerns = []
    if fund.get("pe") is not None and float(fund["pe"]) > 40:
        concerns.append(f"P/E {_num_or_na(fund['pe'], 1)}x")
    if fund.get("roe") is not None and fund["roe"] < 10:
        concerns.append(f"ROE only {_num_1dp(fund['roe'])}%")
    if fund.get("roce") is not None and fund["roce"] < 12:
        concerns.append(f"ROCE only {_num_1dp(fund['roce'])}%")
    cf = fund.get("cash_flow") or {}
    if cf.get("free_cash_flow") is not None and cf["free_cash_flow"] < 0:
        concerns.append(f"Negative FCF of \u20b9{_inr_group(abs(cf['free_cash_flow']))} Cr")
    if cf.get("cfo") is not None and cf["cfo"] < 0:
        concerns.append(f"CFO negative at \u20b9{_inr_group(abs(cf['cfo']))} Cr")
    if fund.get("interest_coverage_ratio") is not None and fund["interest_coverage_ratio"] < 2.5:
        concerns.append(f"Interest coverage only {_num_or_na(fund['interest_coverage_ratio'], 1)}x")
    if price and fund.get("sma_50") is not None and fund.get("sma_200") is not None:
        try:
            if float(price) < float(fund["sma_50"]) and float(price) < float(fund["sma_200"]):
                concerns.append("Price below 50D & 200D SMA")
        except (TypeError, ValueError):
            pass
    if fund.get("macd_line") is not None and fund.get("macd_signal") is not None \
            and fund["macd_line"] < fund["macd_signal"]:
        concerns.append("Bearish MACD")
    fii_delta = _holding_delta(fund.get("fii_pct"))
    if fii_delta is not None and fii_delta < 0:
        concerns.append(f"FII holding declined {abs(fii_delta):.2f}% QoQ")
    if concerns:
        lines.extend(_section("\u26A0\ufe0f", "KEY CONCERNS"))
        for concern in concerns:
            lines.append(f"{_RED} {concern}")
        lines.append("")

    # Key positives
    positives = []
    annuals = fund.get("annuals") or []
    latest = annuals[-1] if annuals else None
    if latest and latest.get("sales_growth") is not None and latest["sales_growth"] > 0:
        positives.append(f"FY{_fy_label(latest.get('year'))[2:]} Sales +{latest['sales_growth']:.1f}%")
    if latest and latest.get("profit_growth") is not None and latest["profit_growth"] > 0:
        positives.append(f"FY{_fy_label(latest.get('year'))[2:]} Profit +{latest['profit_growth']:.1f}%")
    if len(annuals) >= 3 and all(item.get("sales") is not None for item in annuals):
        if annuals[-1]["sales"] > annuals[0]["sales"]:
            positives.append("5-year sales expansion")
    if fund.get("debt_to_equity") is not None and fund["debt_to_equity"] <= 0.5:
        positives.append(f"D/E only {float(fund['debt_to_equity']):.2f}x")
    dii_delta = _holding_delta(fund.get("dii_pct"))
    if dii_delta is not None and dii_delta > 0:
        positives.append("DII holding increased")
    if _consensus_label(fund) in ("Buy", "Strong Buy"):
        positives.append("Analyst consensus Buy")
    if fund.get("target_mean") is not None and price:
        try:
            if float(fund["target_mean"]) > float(price):
                positives.append(f"Mean target \u20b9{_inr_group(fund['target_mean'], 2)}")
        except (TypeError, ValueError):
            pass
    quarters = fund.get("quarters") or []
    if len(quarters) >= 2 and (quarters[-1].get("net_profit") or 0) > 0:
        for quarter in reversed(quarters[:-1]):
            if quarter.get("net_profit") is not None and quarter["net_profit"] < 0:
                positives.append(f"Earnings recovery after {_short_year(quarter.get('quarter'))} loss")
                break
    if positives:
        lines.extend(_section("\u2705", "KEY POSITIVES"))
        for positive in positives:
            lines.append(f"{_GREEN} {positive}")
        lines.append("")

    # Overall view + verdict
    lines.extend(_section("\U0001F3AF", "OVERALL VIEW"))
    for label in ("Business Growth", "Profitability", "Balance Sheet",
                  "Cash Flow", "Valuation", "Technicals", "Analyst View"):
        rating = ratings.get(label)
        if rating:
            icon, word = rating
            lines.append(f"{label}: {icon} {word}")
    lines.append("")
    lines.append(f"\u2B50 <b>VERDICT: {_verdict(ratings)}</b>")
    lines.append("")
    lines.append("<b>Main Question:</b>")
    lines.extend(_main_question(fund, price))
    lines.append("")

    if include_tip:
        lines.append(f"\U0001F4A1 <i>Tip: Track this stock with /addstock {raw_symbol} NSE</i>")
        lines.append("")

    lines.append(_DIVIDER)
    lines.append("\U0001F4CA <b>END OF REPORT</b>")
    lines.append(_DIVIDER)
    return lines
