"""Telegram HTML renderers for the opening/closing session screener.

Pure formatting: takes already-fetched rows and produces HTML lines. Missing
fields render as N/A (never estimated), and each table states Verified: X/10
so a short table is never silently presented as complete.
"""
from __future__ import annotations

import html

_TARGET_PER_TABLE = 10


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


def table(title: str, rows: list[dict], currency: str) -> list[str]:
    """One gainers/losers table with a Verified count."""
    lines = [f"<b>{title}</b>"]
    if not rows:
        lines.append(f"No verified stocks. <b>Verified: 0/{_TARGET_PER_TABLE}</b>")
        return lines
    header = "Rk  Company / Symbol            Price     Chg      Chg%    Volume     VolChg%"
    lines.append(f"<code>{header}</code>")
    for index, row in enumerate(rows, 1):
        name = row.get("name") or row["symbol"]
        label = f"{name[:22]} ({row['symbol']})"
        vol_change = row.get("volume_change_pct")
        vol_change_text = _pct(vol_change) if vol_change is not None else "N/A"
        lines.append(
            "<code>"
            f"{index:>2}. {label:<30} "
            f"{_money(row.get('price'), currency):>9} "
            f"{_amount(row.get('change')):>8} "
            f"{_pct(row.get('change_pct')):>7} "
            f"{_volume(row.get('volume')):>9} "
            f"{vol_change_text:>8}"
            "</code>"
        )
    lines.append(f"<b>Verified: {len(rows)}/{_TARGET_PER_TABLE}</b>")
    return lines


def index_table(market: str, levels: list[dict]) -> list[str]:
    title = "\U0001F1EE\U0001F1F3 Indian indices" if market == "in" else "\U0001F1FA\U0001F1F8 US indices"
    lines = [f"<b>{title}</b>", "<code>Index                 Level        Chg      Chg%</code>"]
    for row in levels:
        level = row.get("level")
        level_text = f"{level:,.2f}" if isinstance(level, (int, float)) else "N/A"
        lines.append(
            "<code>"
            f"{row['label'][:20]:<20} {level_text:>12} "
            f"{_amount(row.get('change')):>9} {_pct(row.get('change_pct')):>7}"
            "</code>"
        )
    return lines


def volume_analysis(rows: list[dict]) -> list[str]:
    """Stocks with unusually strong volume vs the previous session."""
    flagged = [
        r for r in rows
        if r.get("volume_change_pct") is not None and r["volume_change_pct"] > 50
    ]
    lines = ["<b>\U0001F525 Volume analysis</b>"]
    if not flagged:
        lines.append("No stock exceeded +50% volume change on the verified set.")
        return lines
    flagged.sort(key=lambda r: r["volume_change_pct"], reverse=True)
    buckets = (
        ("+200% or more", [r for r in flagged if r["volume_change_pct"] > 200]),
        ("+100% to +200%", [r for r in flagged if 100 < r["volume_change_pct"] <= 200]),
        ("+50% to +100%", [r for r in flagged if 50 < r["volume_change_pct"] <= 100]),
    )
    for label, group in buckets:
        if not group:
            continue
        lines.append(f"<b>{label}</b>")
        for row in group[:10]:
            lines.append(
                "<code>"
                f"{row['symbol']:<12} {_pct(row.get('change_pct')):>7} "
                f"Vol {_volume(row.get('volume')):>9} {_pct(row['volume_change_pct']):>8}"
                "</code>"
            )
    return lines


def catalyst_lines(rows: list[dict], exchange: str, heading: str) -> list[str]:
    """News-based catalyst scan for the biggest movers (factual headlines)."""
    from ..sources.news import get_stock_news

    lines = [f"<b>{heading}</b>"]
    found_any = False
    for row in rows[:4]:
        try:
            items = get_stock_news(exchange, row["symbol"], limit=1)
        except Exception:
            items = []
        if not items:
            continue
        found_any = True
        headline = (items[0].get("title") or "").strip()
        publisher = (items[0].get("publisher") or "").strip()
        source = f" <i>({_escape(publisher)})</i>" if publisher else ""
        lines.append(
            f"\u2022 <b>{_escape(row['symbol'])}</b> {_pct(row.get('change_pct'))} "
            f"\u2014 {_escape(headline[:160])}{source}"
        )
    if not found_any:
        lines.append("No matching news headlines retrieved for the top movers.")
    return lines
