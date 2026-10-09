"""Pure HTML table builders for modern, readable mails.

No network, no disk, no Telegram - every function takes plain dicts/lists
and returns HTML strings (table / section / badge / paragraph).

Compatibility contract (email-client safe):
- Every critical colour, spacing and text style is INLINE on the element.
  Outlook desktop (Word engine) and several gateways strip the document
  <style> block or ignore whole classes - class-only styling loses the
  design there. client.py's <style> block only adds progressive extras.
- Shorthands that reset colours are avoided: use `background-color:` +
  `background-image:` (never bare `background:`), so gradient-ignorant
  clients fall back to the solid colour underneath.
- Layout is tables + block/inline-block elements only (no flex/grid).
"""
from __future__ import annotations

import html as _html

# Critical palette - keep in sync with client.py's <style> block.
_POS = "#059669"    # gains (emerald 600)
_NEG = "#e11d48"    # losses (rose 600)
_INK = "#1e293b"    # body text
_MUTED = "#64748b"  # secondary text
_LINE = "#f1f5f9"   # row separators
_ZEBRA = "#f8fafc"  # even-row tint
_TH_BG = "#f1f5f9"  # header strip
_TH_FG = "#64748b"
_TH_LINE = "#e2e8f0"

_PILL_STYLES = {
    "pos": ("display:inline-block;padding:3px 10px;border-radius:999px;"
            "font-size:12px;font-weight:700;background-color:#dcfce7;"
            "color:#15803d;"),
    "neg": ("display:inline-block;padding:3px 10px;border-radius:999px;"
            "font-size:12px;font-weight:700;background-color:#ffe4e6;"
            "color:#be123c;"),
    "": ("display:inline-block;padding:3px 10px;border-radius:999px;"
         "font-size:12px;font-weight:700;background-color:#f1f5f9;"
         "color:#64748b;"),
}

_TABLE_STYLE = ("style=\"width:100%;border-collapse:collapse;"
                "background-color:#ffffff;font-size:13px;"
                "border:1px solid " + _TH_LINE + ";\"")
_GRID = "border:1px solid " + _TH_LINE + ";"
_TH = ("style=\"background-color:" + _TH_BG + ";color:" + _TH_FG + ";"
       "padding:9px 10px;text-align:left;font-size:11px;font-weight:800;"
       "letter-spacing:0.6px;white-space:nowrap;"
       + _GRID + "\"")
_TH_NUM = ("style=\"background-color:" + _TH_BG + ";color:" + _TH_FG + ";"
           "padding:9px 10px;text-align:right;font-size:11px;font-weight:800;"
           "letter-spacing:0.6px;white-space:nowrap;"
           + _GRID + "\"")


def _td(num: bool = False, bg: str = "") -> str:
    """Inline style for a body cell (optionally right-aligned / tinted).

    Full grid borders on ALL sides - a proper table look. With
    border-collapse the shared edges merge into single lines."""
    style = "padding:9px 10px;" + _GRID + "color:" + _INK + ";"
    if num:
        style += ("text-align:right;white-space:nowrap;"
                  "font-variant-numeric:tabular-nums;")
    if bg:
        style += "background-color:" + bg + ";"
    return "style=\"" + style + "\""


def esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def muted(text: str = "") -> str:
    """Small grey helper line - inline styled so it survives style-stripping.

    `text` must already be escaped/built by the caller (same contract as
    the raw <p class="muted"> strings it replaces).
    """
    return ('<p class="muted" style="margin:8px 0;color:' + _MUTED + ";"
            'font-size:12px;line-height:1.5;">' + text + "</p>")


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
    """Signed change% span - the green/red colour is INLINE (not class-only),
    so moves stay readable in Outlook desktop and other strict clients."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    color = _POS if number >= 0 else _NEG
    cls = move_class(number)
    return (f'<span class="{cls}" style="color:{color};font-weight:700;">'
            f"{esc(_pct(number))}</span>")


def price_cell(value, currency: str = "₹") -> str:
    try:
        return f"{esc(currency)}{_num(value)}"
    except Exception:
        return "-"


def section(title: str, tone: str = "", emoji: str = "") -> str:
    """Modern section header: light strip + colored accent bar (all inline)."""
    accents = {"green": "#10b981", "red": "#f43f5e", "amber": "#f59e0b",
               "slate": "#6366f1", "": "#6366f1"}
    accent = accents.get(tone, accents[""])
    cls = f"rs-sec {tone}".strip()
    prefix = f"{emoji} " if emoji else ""
    return (
        f'<div class="{cls}" style="margin:24px 0 10px;padding:10px 14px;'
        f"background-color:#f8fafc;border-left:4px solid {accent};"
        f'border-radius:0 10px 10px 0;font-size:15px;font-weight:800;'
        f'color:#0f172a;">{prefix}{esc(title)}</div>'
    )


def stat_chips(stats: list[tuple[str, str]]) -> str:
    """Small stat chips row, e.g. [("Verified", "20/20"), ("Markets", "2")].
    Chip box styling is inline (the class only adds polish)."""
    chip_box = ("display:inline-block;padding:6px 12px;margin:0 6px 6px 0;"
                "border-radius:10px;font-size:13px;font-weight:700;"
                "background-color:#f1f5f9;color:#334155;"
                "border:1px solid #e2e8f0;")
    chips = "".join(
        f'<span class="chip" style="{chip_box}">'
        f'<span style="color:{_MUTED};font-weight:400;">{esc(label)}</span> '
        f'<b style="color:#0f172a;">{esc(value)}</b></span>'
        for label, value in stats
    )
    return f'<div style="margin:10px 0 4px;">{chips}</div>'


def stock_table(rows: list[dict], currency: str = "₹", caption: str = "") -> str:
    """Gainers/losers table: Symbol | Company | Price | Chg% | Vol | Vol Chg%."""
    if not rows:
        return muted("No verified stocks.")
    head = (
        f"<tr><th {_TH}>#</th><th {_TH}>Symbol</th><th {_TH}>Company</th>"
        f'<th class="num" {_TH_NUM}>Price</th><th class="num" {_TH_NUM}>Chg%</th>'
        f'<th class="num" {_TH_NUM}>Volume</th><th class="num" {_TH_NUM}>Vol Chg%</th></tr>'
    )
    body_rows = []
    for i, row in enumerate(rows[:10], 1):
        bg = _ZEBRA if i % 2 == 0 else ""
        symbol = esc(row.get("symbol") or "?")
        company = esc(row.get("name") or row.get("company") or symbol)
        price = price_cell(row.get("price"), currency)
        chg = move_cell(row.get("change_pct"))
        vol = esc(_num(row.get("volume"), 0)) if row.get("volume") is not None else "-"
        vol_chg = move_cell(row.get("volume_change_pct"))
        body_rows.append(
            f"<tr><td {_td(bg=bg)}>{i}</td><td {_td(bg=bg)}><b>{symbol}</b></td>"
            f"<td {_td(bg=bg)}>{company}</td>"
            f'<td class="num" {_td(num=True, bg=bg)}>{price}</td>'
            f'<td class="num" {_td(num=True, bg=bg)}>{chg}</td>'
            f'<td class="num" {_td(num=True, bg=bg)}>{vol}</td>'
            f'<td class="num" {_td(num=True, bg=bg)}>{vol_chg}</td></tr>'
        )
    cap = f"<caption style='text-align:left;font-weight:700;'>{esc(caption)}</caption>" if caption else ""
    return (f'<table class="rs-table" {_TABLE_STYLE}>{cap}<thead>{head}</thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>')


def index_table(levels: list[dict]) -> str:
    if not levels:
        return muted("Index levels unavailable.")
    rows = []
    for i, row in enumerate(levels, 1):
        bg = _ZEBRA if i % 2 == 0 else ""
        label = esc(row.get("label") or "?")
        level = esc(_num(row.get("level")))
        chg = move_cell(row.get("change_pct"))
        rows.append(
            f"<tr><td {_td(bg=bg)}><b>{label}</b></td>"
            f"<td class='num' {_td(num=True, bg=bg)}>{level}</td>"
            f"<td class='num' {_td(num=True, bg=bg)}>{chg}</td></tr>"
        )
    return (
        f'<table class="rs-table" {_TABLE_STYLE}><thead><tr><th {_TH}>Index</th>'
        f'<th class="num" {_TH_NUM}>Level</th><th class="num" {_TH_NUM}>Change%</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def watchlist_table(items: list[dict]) -> str:
    """End-of-day store table: Symbol | Price | Day Chg | Verdict pill."""
    if not items:
        return muted("Watchlist is empty - add stocks with /addstock.")
    rows = []
    for i, item in enumerate(items, 1):
        bg = _ZEBRA if i % 2 == 0 else ""
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
            f"<tr><td {_td(bg=bg)}><b>{symbol}</b><br>"
            f"<span style='color:{_MUTED};font-size:12px;'>{company}</span></td>"
            f"<td class='num' {_td(num=True, bg=bg)}>{price}</td>"
            f"<td class='num' {_td(num=True, bg=bg)}>{chg}</td>"
            f"<td {_td(bg=bg)}><span class='pill {pill}' "
            f"style='{_PILL_STYLES.get(pill, _PILL_STYLES[''])}'>{mark}</span></td></tr>"
        )
    return (
        f'<table class="rs-table" {_TABLE_STYLE}><thead><tr><th {_TH}>Stock</th>'
        f'<th class="num" {_TH_NUM}>Price</th><th class="num" {_TH_NUM}>Day Chg</th>'
        f"<th {_TH}>Signal</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def kv_table(pairs: list[tuple[str, str]], caption: str = "") -> str:
    rows = "".join(
        f"<tr><th {_TH}>{esc(k)}</th><td {_td()}>{v}</td></tr>"
        for k, v in pairs
    )
    cap = f"<caption style='text-align:left;font-weight:700;'>{esc(caption)}</caption>" if caption else ""
    return f'<table class="rs-table" {_TABLE_STYLE}>{cap}<tbody>{rows}</tbody></table>'


def buy_by_date(ex_iso) -> str:
    """'Buy by' day for an ex-date (T+1: own the shares a day before ex-date).

    Returns 'DD-Mon' one calendar day earlier, or '' when unparsable. A
    weekend/holiday before the ex-date needs even earlier buying - the
    caller states that caveat next to the table.
    """
    try:
        from datetime import date as _date
        from datetime import timedelta as _td

        day = _date.fromisoformat(str(ex_iso or "").strip()[:10])
        return (day - _td(days=1)).strftime("%d-%b")
    except (TypeError, ValueError):
        return ""


def actions_table(actions: list[dict], limit: int = 12, buy_by: bool = False) -> str:
    if not actions:
        return muted("No upcoming corporate actions for your list.")
    buy_col = f'<th class="num" {_TH_NUM}>Buy by</th>' if buy_by else ""
    rows = []
    for i, action in enumerate(actions[:limit], 1):
        bg = _ZEBRA if i % 2 == 0 else ""
        cells = (
            f"<tr><td {_td(bg=bg)}><b>{esc(action.get('symbol') or '?')}</b></td>"
            f"<td {_td(bg=bg)}>{esc(action.get('subject') or action.get('action') or '')}</td>"
            f"<td class='num' {_td(num=True, bg=bg)}>{esc(action.get('ex_date') or action.get('record_date') or '?')}</td>"
        )
        if buy_by:
            when = buy_by_date(action.get("ex_date"))
            cells += f"<td class='num' {_td(num=True, bg=bg)}>{esc(when) if when else '-'}</td>"
        rows.append(cells + "</tr>")
    return (
        f'<table class="rs-table" {_TABLE_STYLE}><thead><tr><th {_TH}>Symbol</th><th {_TH}>Action</th>'
        f'<th class="num" {_TH_NUM}>Ex-date</th>{buy_col}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def gap_table(rows: list[dict], currency: str = "₹", caption: str = "") -> str:
    """Overnight gaps: Symbol | Open | Gap% | Since open."""
    if not rows:
        return muted("No gaps in this direction.")
    head = (
        f"<tr><th {_TH}>Symbol</th><th {_TH}>Company</th>"
        f'<th class="num" {_TH_NUM}>Open</th><th class="num" {_TH_NUM}>Gap%</th>'
        f'<th class="num" {_TH_NUM}>Since open</th></tr>'
    )
    body = []
    for i, row in enumerate(rows[:10], 1):
        bg = _ZEBRA if i % 2 == 0 else ""
        symbol = esc(row.get("symbol") or "?")
        company = esc(row.get("name") or row.get("company") or symbol)
        open_px = price_cell(row.get("open"), currency)
        gap = move_cell(row.get("gap_pct"))
        move = move_cell(row.get("move_from_open_pct"))
        body.append(
            f"<tr><td {_td(bg=bg)}><b>{symbol}</b></td><td {_td(bg=bg)}>{company}</td>"
            f'<td class="num" {_td(num=True, bg=bg)}>{open_px}</td>'
            f'<td class="num" {_td(num=True, bg=bg)}>{gap}</td>'
            f'<td class="num" {_td(num=True, bg=bg)}>{move}</td></tr>'
        )
    cap = f"<caption style='text-align:left;font-weight:700;'>{esc(caption)}</caption>" if caption else ""
    return (f'<table class="rs-table" {_TABLE_STYLE}>{cap}<thead>{head}</thead>'
            f'<tbody>{"".join(body)}</tbody></table>')
