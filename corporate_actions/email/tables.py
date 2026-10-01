"""Pure HTML table builders for colorful, readable mails.

No network, no disk, no Telegram - every function takes plain dicts/lists
and returns HTML strings (table / section / badge). Gmail strips <style>
classes inconsistently, so all critical colours are ALSO inline.
"""
from __future__ import annotations

import html as _html


def esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _pct(value, digits: int = 2) -> str:
    try:
        number = float(value)
        sign = "+" if number >= 0 else ""
        return f"{sign}{number:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def move_class(value) -> str:
    try:
        return "pos" if float(value) >= 0 else "neg"
    except (TypeError, ValueError):
        return ""


def move_cell(value) -> str:
    cls = move_class(value)
    return f'<span class="{cls}">{esc(_pct(value))}</span>' if cls else "-"


def price_cell(value, currency: str = "₹") -> str:
    try:
        return f"{esc(currency)}{_num(value)}"
    except Exception:
        return "-"


def section(title: str, tone: str = "", emoji: str = "") -> str:
    """Colored section banner: tone in (green, red, amber, slate, '')."""
    cls = f"rs-sec {tone}".strip()
    prefix = f"{emoji} " if emoji else ""
    return f'<div class="{cls}">{prefix}{esc(title)}</div>'


def stock_table(rows: list[dict], currency: str = "₹", caption: str = "") -> str:
    """Gainers/losers table: Symbol | Company | Price | Chg | Chg% | Vol."""
    if not rows:
        return '<p class="muted">No verified stocks.</p>'
    head = (
        "<tr><th>#</th><th>Symbol</th><th>Company</th>"
        '<th class="num">Price</th><th class="num">Chg%</th>'
        '<th class="num">Volume</th><th class="num">Vol Chg%</th></tr>'
    )
    body_rows = []
    for i, row in enumerate(rows[:10], 1):
        symbol = esc(row.get("symbol") or "?")
        company = esc(row.get("name") or row.get("company") or symbol)
        price = price_cell(row.get("price"), currency)
        chg = move_cell(row.get("change_pct"))
        vol = esc(_num(row.get("volume"), 0)) if row.get("volume") is not None else "-"
        vol_chg = move_cell(row.get("volume_change_pct"))
        body_rows.append(
            f"<tr><td>{i}</td><td><b>{symbol}</b></td><td>{company}</td>"
            f'<td class="num">{price}</td><td class="num">{chg}</td>'
            f'<td class="num">{vol}</td><td class="num">{vol_chg}</td></tr>'
        )
    cap = f"<caption style='text-align:left;font-weight:700;'>{esc(caption)}</caption>" if caption else ""
    return f'<table class="rs-table">{cap}<thead>{head}</thead><tbody>{"".join(body_rows)}</tbody></table>'


def index_table(levels: list[dict]) -> str:
    if not levels:
        return '<p class="muted">Index levels unavailable.</p>'
    rows = []
    for row in levels:
        label = esc(row.get("label") or "?")
        level = esc(_num(row.get("level")))
        chg = move_cell(row.get("change_pct"))
        rows.append(f"<tr><td><b>{label}</b></td><td class='num'>{level}</td><td class='num'>{chg}</td></tr>")
    return (
        '<table class="rs-table"><thead><tr><th>Index</th>'
        '<th class="num">Level</th><th class="num">Change%</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def watchlist_table(items: list[dict]) -> str:
    """End-of-day store table: Symbol | Price | Day Chg | Verdict pill."""
    if not items:
        return '<p class="muted">Watchlist is empty - add stocks with /addstock.</p>'
    rows = []
    for item in items:
        symbol = esc(item.get("symbol") or "?")
        company = esc(item.get("company") or "")
        price = price_cell(item.get("price"), item.get("currency", "₹"))
        chg = move_cell(item.get("change_pct"))
        try:
            pct = float(item.get("change_pct"))
            pill = "pos" if pct >= 0 else "neg"
            mark = "🟢" if pct >= 2 else ("🔴" if pct <= -2 else "🟡")
        except (TypeError, ValueError):
            pill, mark = "", "⚪"
        rows.append(
            f"<tr><td><b>{symbol}</b><br><span class='muted'>{company}</span></td>"
            f"<td class='num'>{price}</td><td class='num'>{chg}</td>"
            f"<td><span class='pill {pill}'>{mark}</span></td></tr>"
        )
    return (
        '<table class="rs-table"><thead><tr><th>Stock</th>'
        '<th class="num">Price</th><th class="num">Day Chg</th><th>Signal</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def kv_table(pairs: list[tuple[str, str]], caption: str = "") -> str:
    rows = "".join(
        f"<tr><th style='text-align:left;background:#f1f5f9;color:#0f172a;'>{esc(k)}</th><td>{v}</td></tr>"
        for k, v in pairs
    )
    cap = f"<caption style='text-align:left;font-weight:700;'>{esc(caption)}</caption>" if caption else ""
    return f'<table class="rs-table">{cap}<tbody>{rows}</tbody></table>'


def actions_table(actions: list[dict], limit: int = 12) -> str:
    if not actions:
        return '<p class="muted">No upcoming corporate actions for your list.</p>'
    rows = []
    for action in actions[:limit]:
        rows.append(
            f"<tr><td><b>{esc(action.get('symbol') or '?')}</b></td>"
            f"<td>{esc(action.get('subject') or action.get('action') or '')}</td>"
            f"<td class='num'>{esc(action.get('ex_date') or action.get('record_date') or '?')}</td></tr>"
        )
    return (
        '<table class="rs-table"><thead><tr><th>Symbol</th><th>Action</th>'
        '<th class="num">Ex-date</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
