"""Pure screener.in HTML parsers (no network, no pacing, no caching).

Every function here is a deterministic transform of an HTML string into a
plain dict/float/list - unit-testable with canned pages and safe to call from
any thread. The fetch, rate-limit pacing, circuit breaker and caching live in
screener.py, which feeds pages into these parsers.
"""
from __future__ import annotations

import html
import re


def parse_chg(curr_str, prev_str) -> str:
    """Append the QoQ change as a colour-coded (emoji) delta, e.g. '50.2% (\U0001F7E2\u25b2+0.48%)'."""
    try:
        current_price = float(re.sub(r"[^\d\.-]", "", curr_str))
        previous_price = float(re.sub(r"[^\d\.-]", "", prev_str))
        diff = round(current_price - previous_price, 2)
        if diff > 0:
            return f"{curr_str} (\U0001F7E2\u25b2+{diff:.2f}%)"
        elif diff < 0:
            return f"{curr_str} (\U0001F534\u25bc{diff:.2f}%)"
        else:
            return curr_str
    except Exception:
        return curr_str


def _extract_table(page: str, section_id: str) -> str:
    """The <table>...</table> fragment of a screener.in section ('' when absent)."""
    index = page.find(f'id="{section_id}"')
    if index < 0:
        return ""
    start = page.find("<table", index)
    if start < 0:
        return ""
    end = page.find("</table>", start)
    if end < 0:
        return ""
    return page[start:end]


def _table_headers(table: str) -> list[str]:
    """Column headers of a screener.in table (skips the empty label header)."""
    thead = re.search(r"<thead[^>]*>(.*?)</thead>", table, re.S)
    if not thead:
        return []
    headers = []
    for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", thead.group(1), re.S):
        text = re.sub(r"<[^>]+>|\s+", " ", html.unescape(cell)).strip()
        if text:
            headers.append(text)
    return headers


def _table_rows(table: str) -> list[tuple[str, list[str]]]:
    """(label, [values...]) rows of a screener.in table (values exclude the label cell)."""
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        label_match = re.search(r'<td class="text"[^>]*>(.*?)</td>', row, re.S)
        cells = [
            re.sub(r"<[^>]+>|\s+", " ", html.unescape(cell)).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if len(cells) < 2:
            continue
        label = (
            re.sub(r"<[^>]+>|\s+", " ", html.unescape(label_match.group(1))).strip()
            if label_match
            else ""
        )
        rows.append((label, cells[1:]))
    return rows


def _to_number(text: str) -> float | None:
    """Parse a screener.in cell ("8,82,886" | "72.30%" | "-4,847") into a float."""
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _yoy(current: float | None, previous: float | None) -> float | None:
    """Year-on-year % change; None when the base is missing or zero."""
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def _latest_number(values: list[str]) -> float | None:
    """The most recent (rightmost) number of a table row."""
    numbers = [_to_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    return numbers[-1] if numbers else None


def _pick(latest: dict, prefix: str) -> float | None:
    """Look up a row by label prefix in a {label: value} dict (labels carry ' +' noise)."""
    for label, value in latest.items():
        if label.startswith(prefix):
            return value
    return None


def parse_sector_pe_table(page: str) -> float | None:
    """Average P/E of a screener.in sector page, from its constituent list."""
    table = re.search(r"<table[^>]*>(.*?)</table>", page, re.S)
    if not table:
        return None
    values = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S)[1:]:
        cells = [
            re.sub(r"<[^>]+>|\s+", " ", cell).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        ]
        if len(cells) >= 4 and cells[3]:
            try:
                value = float(cells[3].replace(",", ""))
                if value > 0:
                    values.append(value)
            except ValueError:
                continue
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def parse_sector_link(page: str) -> tuple[str, str] | None:
    """The (sector_name, market_slug) of a company page, or None."""
    match = re.search(r'<p class="sub">(.*?)</p>', page, re.S)
    if not match:
        return None
    for link in re.finditer(
        r'<a href="(/market/[^"]+)"[^>]*title="Sector">(.*?)</a>',
        match.group(1),
        re.S,
    ):
        sector = html.unescape(
            re.sub(r"<[^>]+>|\s+", " ", link.group(2)).strip()
        )
        return sector, link.group(1)
    return None


def parse_deep_tables(page: str) -> dict:
    """Annual P&L trend, quarterly results, balance sheet and cash flow from screener.in.

    Mirrors the web dashboard's stock-details enrichment. All money values are
    in ₹ Crore (screener.in units):
      #profit-loss    -> annual Sales/OPM/Net Profit/EPS + YoY growth, interest coverage
      #quarters       -> recent quarterly results (last 8 filled)
      #balance-sheet  -> latest fiscal-year net worth / borrowings / assets
      #cash-flow      -> latest fiscal-year CFO / CFI / CFF / FCF
    """
    out: dict = {}

    table = _extract_table(page, "profit-loss")
    if table:
        # Headers end with a trailing "TTM" column - keep only fiscal years.
        headers = [header for header in _table_headers(table) if header != "TTM"]
        rows = dict(_table_rows(table))

        def row_for(prefix: str):
            for label, values in rows.items():
                if label.startswith(prefix):
                    return values
            return None

        sales, operating, opm = row_for("Sales"), row_for("Operating Profit"), row_for("OPM %")
        interest, net_profit, eps = row_for("Interest"), row_for("Net Profit"), row_for("EPS")
        annuals = []
        for index, year in enumerate(headers):
            sales_value = _to_number(sales[index]) if sales and index < len(sales) else None
            operating_value = _to_number(operating[index]) if operating and index < len(operating) else None
            interest_value = _to_number(interest[index]) if interest and index < len(interest) else None
            net_value = _to_number(net_profit[index]) if net_profit and index < len(net_profit) else None
            previous_sales = _to_number(sales[index - 1]) if sales and index > 0 else None
            previous_net = _to_number(net_profit[index - 1]) if net_profit and index > 0 else None
            coverage = None
            if operating_value is not None and interest_value and interest_value > 0:
                coverage = round(operating_value / interest_value, 1)
            annuals.append({
                "year": year,
                "sales": sales_value,
                "opm": _to_number(opm[index]) if opm and index < len(opm) else None,
                "net_profit": net_value,
                "eps": _to_number(eps[index]) if eps and index < len(eps) else None,
                "sales_growth": _yoy(sales_value, previous_sales),
                "profit_growth": _yoy(net_value, previous_net),
                "interest_coverage": coverage,
            })
        # Attach ROCE % per year from the #ratios table (same fiscal-year columns).
        ratios_table = _extract_table(page, "ratios")
        if ratios_table:
            ratio_headers = _table_headers(ratios_table)
            for label, values in _table_rows(ratios_table):
                if label == "ROCE %":
                    for item in annuals:
                        if item["year"] in ratio_headers:
                            item["roce"] = _to_number(values[ratio_headers.index(item["year"])])
        if annuals:
            out["annuals"] = annuals
            latest = annuals[-1]
            if latest["interest_coverage"] is not None:
                out["interest_coverage_ratio"] = latest["interest_coverage"]

    table = _extract_table(page, "quarters")
    if table:
        headers = _table_headers(table)
        rows = dict(_table_rows(table))

        def q_row_for(prefix: str):
            for label, values in rows.items():
                if label.startswith(prefix):
                    return values
            return None

        q_sales, q_opm = q_row_for("Sales"), q_row_for("OPM %")
        q_net, q_eps = q_row_for("Net Profit"), q_row_for("EPS")
        quarters = []
        for index, quarter in enumerate(headers):
            quarters.append({
                "quarter": quarter,
                "sales": _to_number(q_sales[index]) if q_sales and index < len(q_sales) else None,
                "opm": _to_number(q_opm[index]) if q_opm and index < len(q_opm) else None,
                "net_profit": _to_number(q_net[index]) if q_net and index < len(q_net) else None,
                "eps": _to_number(q_eps[index]) if q_eps and index < len(q_eps) else None,
            })
        filled = [q for q in quarters if q["sales"] is not None or q["net_profit"] is not None]
        if filled:
            out["quarters"] = filled[-8:]

    table = _extract_table(page, "balance-sheet")
    if table:
        headers = _table_headers(table)
        latest = {label: _latest_number(values) for label, values in _table_rows(table)}
        equity, reserves = _pick(latest, "Equity Capital"), _pick(latest, "Reserves")
        net_worth = round(equity + reserves, 1) if equity is not None and reserves is not None else None
        out["balance_sheet"] = {
            "year": headers[-1] if headers else "Latest",
            "net_worth": net_worth,
            "borrowings": _pick(latest, "Borrowings"),
            "total_liabilities": _pick(latest, "Total Liabilities"),
            "fixed_assets": _pick(latest, "Fixed Assets"),
            "investments": _pick(latest, "Investments"),
            "total_assets": _pick(latest, "Total Assets"),
        }

    table = _extract_table(page, "cash-flow")
    if table:
        headers = _table_headers(table)
        latest = {label: _latest_number(values) for label, values in _table_rows(table)}
        out["cash_flow"] = {
            "year": headers[-1] if headers else "Latest",
            "cfo": _pick(latest, "Cash from Operating"),
            "cfi": _pick(latest, "Cash from Investing"),
            "cff": _pick(latest, "Cash from Financing"),
            "net_cash_flow": _pick(latest, "Net Cash Flow"),
            "free_cash_flow": _pick(latest, "Free Cash Flow"),
        }

    return out


def parse_top_ratios(page: str) -> dict:
    """Top-ratios block (Stock P/E, Market Cap, Div Yield, D/E, High/Low, ROCE, ROE)."""
    out: dict = {}
    index = page.find('id="top-ratios"')
    if index <= 0:
        return out
    chunk = page[index:index + 3000]
    for list_item in re.findall(r'<li[^>]*>(.*?)</li>', chunk, re.S):
        name_m = re.search(r'<span class="name"[^>]*>(.*?)</span>', list_item, re.S)
        num_m = re.findall(r'<span class="number"[^>]*>(.*?)</span>', list_item, re.S)
        if name_m and num_m:
            name = re.sub(r'<[^>]+>|\s+', ' ', name_m.group(1)).strip().lower()
            vals = [
                re.sub(r'<[^>]+>|\s+|,|₹', '', str(number_match)).strip()
                for number_match in num_m
            ]
            if vals and vals[0]:
                try:
                    if 'stock p/e' in name or name == 'p/e':
                        out['pe'] = float(vals[0])
                    elif 'dividend yield' in name:
                        out['div_yield'] = float(vals[0])
                    elif 'debt to equity' in name:
                        out['debt_to_equity'] = float(vals[0])
                    elif 'roce' in name:
                        out['roce'] = float(vals[0])
                    elif 'roe' in name:
                        out['roe'] = float(vals[0])
                    elif 'market cap' in name:
                        out['market_cap'] = float(vals[0])
                    elif 'high / low' in name or 'high/low' in name:
                        if len(vals) >= 2 and vals[1]:
                            out['wk52_high'] = float(vals[0])
                            out['wk52_low'] = float(vals[1])
                except (ValueError, IndexError):
                    pass
    return out


def parse_shareholding(page: str) -> dict:
    """Quarterly shareholding pattern (promoter/FII/DII/public) with QoQ deltas."""
    out: dict = {}
    index = page.find('<div id="quarterly-shp"')
    end_index = page.find('<div id="yearly-shp"')
    segment = page[index:end_index] if index > 0 and end_index > index else ""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", segment, re.S):
        first = re.search(r'<td class="text">(.*?)</td>', row, re.S)
        if not first:
            continue
        label = re.sub(r"<[^>]+>|\s+", " ", first.group(1)).strip().lower()
        cells = [
            re.sub(r"<[^>]+>|\s+", " ", cell).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if cells:
            curr = cells[-1]
            prev = cells[-2] if len(cells) >= 2 else None
            val_str = parse_chg(curr, prev) if prev else curr
            if label.startswith("promoter"):
                out["promoter_pct"] = val_str
            elif label.startswith("fii"):
                out["fii_pct"] = val_str
            elif label.startswith("dii"):
                out["dii_pct"] = val_str
            elif label.startswith("public"):
                out["public_pct"] = val_str
    return out


def parse_page(page: str) -> dict | None:
    """All enrichments from one screener.in company page (pure, no fetches).

    Returns the parsed fields plus 'sector_slug' so the caller can resolve the
    sector P/E (which requires an extra network fetch + cache). None when the
    page yields nothing.
    """
    out: dict = {}
    deep = parse_deep_tables(page)
    if deep:
        out.update(deep)
    sector = parse_sector_link(page)
    if sector:
        out["sector"], out["sector_slug"] = sector
    out.update(parse_top_ratios(page))
    out.update(parse_shareholding(page))
    return out or None
