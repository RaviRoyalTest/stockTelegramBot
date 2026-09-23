"""Telegram HTML renderers for the opening/closing session screener.

Pure formatting: takes already-fetched rows and produces HTML lines. Missing
fields render as N/A (never estimated), and each table states Verified: X/10
so a short table is never silently presented as complete.

Layout rules (learned the hard way - the old fixed-width grid broke the
moment a company name exceeded its column, shoving every number sideways):

* Company names are NEVER truncated - the full name sits on its own line.
* Numbers always go on short, token-separated metric lines (~40 columns
  max) so a phone wrap can only land BETWEEN tokens, never inside one.
* Cards are PLAIN TEXT, not monospace <code>: inline code renders as a
  solid blue wall in several Telegram themes ("everything is blue").
  Color comes from semantic emoji instead - medal/keycap ranks, green/red
  direction dots, check/warn/fires - which survive every theme.
* The direction dot (+green/-red) opens every metrics line, so a wrapped
  or scrolled card still shows its sign at a glance.
"""
from __future__ import annotations

import html

_TARGET_PER_TABLE = 10
_MAX_NAME_LINE = 44   # rank token + full company name + (SYMBOL)
_MAX_DATA_LINE = 46   # indent + pct + price + volume tokens
_ARROW_UP = "\u25b2"  # ▲
_ARROW_DOWN = "\u25bc"  # ▼
_INDENT = "\u3000"     # ideographic space: indents metrics under the name
_RANK_EMOJIS = {
    1: "\U0001F947",  # 🥇
    2: "\U0001F948",  # 🥈
    3: "\U0001F949",  # 🥉
    4: "4\u20e3", 5: "5\u20e3", 6: "6\u20e3",
    7: "7\u20e3", 8: "8\u20e3", 9: "9\u20e3",
    10: "\U0001F51F",  # 🔟
}


def _rank_token(index: int) -> str:
    """🥇🥈🥉 then keycaps 4️⃣-🔟; plain '11.' beyond (tables target 10)."""
    return _RANK_EMOJIS.get(index, f"{index}.")


def _dir_token(value) -> str:
    """Direction dot for any signed value: 🟢 up / 🔴 down / ⚪ unknown."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "\u26aa"  # ⚪
    if number > 0:
        return "\U0001F7E2"  # 🟢
    if number < 0:
        return "\U0001F534"  # 🔴
    return "\u26aa"


def _verified_line(count: int) -> str:
    """✅ when the table is complete, ⚠️ when it falls short of target."""
    mark = "\u2705" if count >= _TARGET_PER_TABLE else "\u26a0\ufe0f"
    return f"{mark} <b>Verified: {count}/{_TARGET_PER_TABLE}</b>"


def _escape(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _amount(value) -> str:
    """Signed price change with 2 decimals, e.g. '+12.35' / '-4.10'."""
    try:
        return f"{float(value):+,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _pct(value) -> str:
    """Signed percent with 2 decimals, e.g. '+3.21%'."""
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def _volume(value) -> str:
    """Compact volume: 12.3M / 1.2B."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if number >= 1e9:
        return f"{number / 1e9:,.2f}B"
    if number >= 1e6:
        return f"{number / 1e6:,.2f}M"
    if number >= 1e3:
        return f"{number / 1e3:,.1f}K"
    return f"{number:,.0f}"


def _money(price, currency: str) -> str:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return "N/A"
    symbol = "\u20b9" if currency == "INR" else "$"
    return f"{symbol}{value:,.2f}"


# ------------------------------------------------------------- stock rows ---

def _trim_name(name: str, budget: int) -> str:
    """Collapse whitespace and, only if truly unavoidable, cut at a word."""
    name = " ".join(str(name).split())
    if len(name) <= budget:
        return name
    cut = name[: max(budget, 8)].rsplit(" ", 1)[0].strip(" ,.-")
    return cut or name[: max(budget, 8)]


def _stock_name_line(index: int, row: dict) -> str:
    """``🥇 Solar Industries India (SOLARINDS)`` - full name, never cut.

    The visible text is HTML-escaped AFTER the width budget is applied, so
    escaping never pushes a line past the phone-wrap limit - important for
    names like ``Larsen & Toubro`` / symbols like ``M&M`` (a bare ``&``
    makes Telegram's HTML parser reject the whole chunk).
    """
    rank = f"{_rank_token(index)} "
    name = " ".join(str(row.get("name") or row["symbol"]).split())
    symbol = str(row["symbol"]).strip()
    suffix = "" if name.lower() == symbol.lower() else f" ({symbol})"
    budget = _MAX_NAME_LINE - len(rank) - len(suffix)
    text = _trim_name(name, max(budget, 8))
    return f"{rank}{_escape(text)}{_escape(suffix)}"


def _vol_change_token(row: dict) -> str:
    """``Vol ▲34%`` / ``Vol ▼12%`` / ``Vol N/A`` - never estimated."""
    value = row.get("volume_change_pct")
    if value is None:
        return "Vol N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Vol N/A"
    arrow = _ARROW_UP if number >= 0 else _ARROW_DOWN
    return f"Vol {arrow}{abs(number):,.0f}%"


def _stock_metrics_line(row: dict, currency: str) -> str:
    """``　🟢 +3.24% · ₹19,890.00 · Vol ▲84%`` - colored, token-separated.

    The direction dot leads so the sign survives any wrap; tokens are kept
    short so the whole line fits a phone without wrapping mid-number.
    """
    return (
        f"{_INDENT}{_dir_token(row.get('change_pct'))} {_pct(row.get('change_pct'))}"
        f" \u00b7 {_money(row.get('price'), currency)}"
        f" \u00b7 {_vol_change_token(row)}"
    )


def table(rows: list[dict], currency: str) -> list[str]:
    """One gainers/losers block: plain-text card per stock + verified mark."""
    lines: list[str] = []
    if not rows:
        lines.append(f"No verified stocks. {_verified_line(0)}")
        return lines
    for index, row in enumerate(rows, 1):
        lines.append(_stock_name_line(index, row))
        lines.append(_stock_metrics_line(row, currency))
    lines.append(_verified_line(len(rows)))
    return lines


def index_table(levels: list[dict]) -> list[str]:
    """One colored line per index: ``🟢 Nifty 50 25,506.00 +85.10 (+0.33%)``."""
    lines: list[str] = []
    for row in levels:
        label = str(row["label"])[:20]
        level = row.get("level")
        if isinstance(level, (int, float)):
            lines.append(
                f"{_dir_token(row.get('change_pct'))} <b>{_escape(label)}</b> "
                f"{level:,.2f} {_amount(row.get('change'))}"
                f" ({_pct(row.get('change_pct'))})"
            )
        else:
            lines.append(f"\u26aa <b>{_escape(label)}</b>: N/A")
    if not lines:
        lines.append("Index levels unavailable.")
    return lines


# --------------------------------------------------------- volume flagged ---

def volume_flagged(rows: list[dict]) -> list[dict]:
    """Rows whose volume change exceeded +50%, strongest first."""
    flagged = [
        r for r in rows
        if r.get("volume_change_pct") is not None and r["volume_change_pct"] > 50
    ]
    flagged.sort(key=lambda r: r["volume_change_pct"], reverse=True)
    return flagged


def volume_buckets(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Flagged rows grouped into the +50/+100/+200 bands, strongest bands first."""
    flagged = volume_flagged(rows)
    buckets = (
        ("+200% or more", [r for r in flagged if r["volume_change_pct"] > 200]),
        ("+100% to +200%", [r for r in flagged if 100 < r["volume_change_pct"] <= 200]),
        ("+50% to +100%", [r for r in flagged if 50 < r["volume_change_pct"] <= 100]),
    )
    return [(label, group) for label, group in buckets if group]


def volume_buckets_payload(rows: list[dict]) -> list[dict]:
    """JSON-friendly shape of volume_buckets for the web dashboard."""
    return [
        {"label": label, "rows": [dict(row) for row in group]}
        for label, group in volume_buckets(rows)
    ]


def _volume_scan_line(row: dict) -> str:
    """``• SOLARINDS 🟢 +3.24% · Vol 0.40M · ▲84%`` - one colored line."""
    symbol = str(row.get("symbol") or "")[:12]
    try:
        arrow = _ARROW_UP if float(row["volume_change_pct"]) >= 0 else _ARROW_DOWN
        vol_pct = f"{arrow}{abs(float(row['volume_change_pct'])):,.0f}%"
    except (TypeError, ValueError, KeyError):
        vol_pct = "N/A"
    return (
        f"\u2022 <b>{_escape(symbol)}</b> {_dir_token(row.get('change_pct'))} "
        f"{_pct(row.get('change_pct'))} \u00b7 Vol {_volume(row.get('volume'))}"
        f" \u00b7 {vol_pct}"
    )


def volume_analysis_payload(buckets: list[dict]) -> list[str]:
    """Telegram rendering of a volume_buckets_payload (no heading)."""
    lines: list[str] = []
    for bucket in buckets:
        count = len(bucket["rows"])
        lines.append(f"\U0001F525 <b>{_escape(bucket['label'])}</b> \u00b7 {count} stock(s)")
        for row in bucket["rows"][:10]:
            lines.append(_volume_scan_line(row))
    return lines


def volume_analysis(rows: list[dict]) -> list[str]:
    """Stocks with unusually strong volume vs the previous session."""
    lines = ["<b>\U0001F525 Volume analysis</b>"]
    buckets = volume_buckets(rows)
    if not buckets:
        lines.append("No stock exceeded +50% volume change on the verified set.")
        return lines
    lines.extend(volume_analysis_payload(
        [{"label": label, "rows": group} for label, group in buckets]
    ))
    return lines


# --------------------------------------------------------------- catalysts --

def catalyst_items(rows: list[dict], exchange: str, max_rows: int = 4) -> list[dict]:
    """News-based catalyst scan for the biggest movers (factual headlines).

    JSON-friendly dicts so both the Telegram renderer and the web dashboard
    consume the same lookup: {symbol, change_pct, headline, publisher}.
    """
    from ..sources.news import get_stock_news

    items: list[dict] = []
    for row in rows[:max_rows]:
        try:
            news = get_stock_news(exchange, row["symbol"], limit=1)
        except Exception:
            news = []
        if not news:
            continue
        items.append({
            "symbol": row["symbol"],
            "change_pct": row.get("change_pct"),
            "headline": (news[0].get("title") or "").strip()[:160],
            "publisher": (news[0].get("publisher") or "").strip(),
        })
    return items


def catalyst_lines_from_items(items: list[dict], heading: str) -> list[str]:
    """Telegram rendering of a catalyst_items payload."""
    lines = [f"<b>{heading}</b>"]
    for item in items:
        source = f" <i>({_escape(item['publisher'])})</i>" if item["publisher"] else ""
        lines.append(
            f"\u2022 {_dir_token(item.get('change_pct'))} <b>{_escape(item['symbol'])}</b>"
            f" {_pct(item.get('change_pct'))} \u2014 {_escape(item['headline'])}{source}"
        )
    if not items:
        lines.append("No matching news headlines retrieved for the top movers.")
    return lines


def catalyst_lines(rows: list[dict], exchange: str, heading: str) -> list[str]:
    """Telegram rendering of catalyst_items."""
    return catalyst_lines_from_items(catalyst_items(rows, exchange), heading)
