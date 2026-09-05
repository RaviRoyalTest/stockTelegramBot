"""Fundamental screener command: /screen (web Screener parity).

Filters the NIFTY universe by valuation, quality, yield and momentum using
the same screener_service the web /market page uses — same filters, same
rows, same sorting. Examples:

  /screen pe<25 roe>15            → value screen, top 10 by market cap
  /screen div>1.5 pe<25           → dividend screen
  /screen rsi 50-70 macd          → momentum screen (RSI band + MACD bullish)
  /screen debt<0.5 roce>18        → quality screen
  /screen sector bank n 15        → 15 banking stocks
  /screen pe<20 sort pe            → cheapest first
"""
from __future__ import annotations

import logging
import re

from ..core.text import escape, split_messages
from ..screener_service import screen_universe
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

SCREEN_USAGE = (
    "<b>/screen</b> - fundamental stock screener (same engine as the web Screener)\n"
    "<code>/screen pe&lt;25 roe&gt;15</code>  \u2192 value screen, top 10 by market cap\n"
    "<code>/screen div&gt;1.5 pe&lt;25</code>  \u2192 dividend screen\n"
    "<code>/screen rsi 50-70 macd</code>  \u2192 momentum (RSI band + MACD bullish)\n"
    "<code>/screen debt&lt;0.5 roce&gt;18</code>  \u2192 quality screen\n"
    "<code>/screen sector bank n 15</code>  \u2192 15 banking stocks\n"
    "<code>/screen pe&lt;20 sort pe</code>  \u2192 cheapest first\n"
    "Filters: <code>pe&lt;25</code> <code>pe 10-25</code> <code>roe&gt;15</code> "
    "<code>roce&gt;15</code> <code>div&gt;1.5</code> <code>debt&lt;0.5</code> "
    "<code>mcap&gt;5000</code> (\u20b9 Cr) <code>price&gt;100</code> <code>price 100-500</code> "
    "<code>rsi&gt;50</code> <code>rsi 50-70</code> <code>change&gt;2</code> "
    "<code>sector bank</code> <code>macd</code> <code>ema200</code> "
    "<code>sort pe|roe|mcap|price|rsi|change</code> <code>n 15</code> "
    "<code>nifty100</code> (default: nifty500)"
)

_SORT_KEYS = {
    "pe": "pe", "roe": "roe", "roce": "roce", "mcap": "market_cap",
    "marketcap": "market_cap", "price": "price", "rsi": "rsi",
    "change": "change_pct", "chg": "change_pct", "div": "div_yield",
    "debt": "debt_to_equity", "sector": "sector", "symbol": "symbol",
}


def _parse_num(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_screen_args(tokens: list[str]) -> tuple[dict, str, bool, int, str]:
    """Parse /screen tokens → (filters, sort, ascending, limit, universe)."""
    filters: dict = {}
    sort, ascending, limit, universe = "market_cap", False, 10, "nifty500"
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        low = tok.lower()
        if low in ("nifty100", "n100"):
            universe = "nifty100"
        elif low in ("nifty500", "n500"):
            universe = "nifty500"
        elif low in ("macd", "macdbull"):
            filters["require_macd_bull"] = True
        elif low in ("ema200", "ema", "dma", "200dma", "above200"):
            filters["require_above_ema200"] = True
        elif low == "sort" and i + 1 < len(tokens):
            key = tokens[i + 1].lower()
            if key in _SORT_KEYS:
                sort = _SORT_KEYS[key]
                ascending = key in ("pe", "debt")
            i += 1
        elif low in ("n", "count", "top", "limit") and i + 1 < len(tokens):
            number = _parse_num(tokens[i + 1])
            if number is not None:
                limit = max(1, min(30, int(number)))
            i += 1
        elif low == "sector" and i + 1 < len(tokens):
            filters["sector"] = tokens[i + 1]
            i += 1
        else:
            _parse_filter_token(tok, low, tokens, i, filters)
            # range form may consume the next token ("rsi 50-70")
            if low in ("pe", "roe", "roce", "div", "rsi", "price", "change", "mcap") \
                    and i + 1 < len(tokens) and re.fullmatch(r"[\d.,]+\s*-\s*[\d.,]+", tokens[i + 1]):
                _parse_filter_token(low + tokens[i + 1].replace(" ", ""), low, tokens, i, filters)
                i += 1
        i += 1
    return filters, sort, ascending, limit, universe


def _parse_filter_token(tok: str, low: str, tokens: list[str], i: int, filters: dict) -> None:
    """Parse one comparison token (pe<25, roe>15, rsi50-70) into filters."""
    text = (tok or "").lower()
    match = re.fullmatch(r"(pe|roe|roce|div|debt|mcap|price|rsi|change)\s*([<>]=?|=)\s*([\d.,]+)", text)
    if match:
        name, op, raw = match.groups()
        value = _parse_num(raw)
        if value is None:
            return
        _apply_bound(filters, name, op, value)
        return
    match = re.fullmatch(r"(pe|roe|roce|div|debt|mcap|price|rsi|change)\s*([\d.,]+)\s*-\s*([\d.,]+)", text)
    if match:
        name, raw_lo, raw_hi = match.groups()
        lo, hi = _parse_num(raw_lo), _parse_num(raw_hi)
        if lo is None or hi is None:
            return
        _apply_bound(filters, name, ">=", lo)
        _apply_bound(filters, name, "<=", hi)


_BOUND_MAP = {
    "pe": ("pe_min", "pe_max"),
    "roe": ("roe_min", "roe_max"),
    "roce": ("roce_min", None),
    "div": ("div_yield_min", None),
    "debt": (None, "debt_to_equity_max"),
    "mcap": ("market_cap_min", "market_cap_max"),
    "price": ("price_min", "price_max"),
    "rsi": ("rsi_min", "rsi_max"),
    "change": ("change_pct_min", "change_pct_max"),
}


def _apply_bound(filters: dict, name: str, op: str, value: float) -> None:
    lo_key, hi_key = _BOUND_MAP[name]
    if op in (">", ">=") and lo_key:
        # keep the tightest lower bound
        if filters.get(lo_key) is None or value > filters[lo_key]:
            filters[lo_key] = value
    elif op in ("<", "<=") and hi_key:
        if filters.get(hi_key) is None or value < filters[hi_key]:
            filters[hi_key] = value
    elif op == "=":
        if lo_key:
            filters[lo_key] = value
        if hi_key:
            filters[hi_key] = value


def _format_row(index: int, row: dict) -> list[str]:
    """One compact screen-hit block (never drops the symbol link)."""
    symbol = row.get("symbol") or "?"
    company = row.get("company") or symbol
    bits = [f"<b>{index}. {escape(symbol)}</b> \u2014 {escape(str(company))}"]
    price, change = row.get("price"), row.get("change_pct")
    if price is not None:
        head = f"  \u20b9{price:,.1f}"
        if change is not None:
            icon = "\U0001F7E2" if change >= 0 else "\U0001F534"
            head += f"  {icon} {change:+.2f}%"
        bits.append(head)
    stats = []
    if row.get("pe") is not None:
        stats.append(f"P/E {row['pe']:.1f}")
    if row.get("roe") is not None:
        stats.append(f"ROE {row['roe']:.1f}%")
    if row.get("div_yield") is not None:
        stats.append(f"Div {row['div_yield']:.2f}%")
    if row.get("market_cap") is not None:
        stats.append(f"MCap \u20b9{row['market_cap']:,.0f}Cr")
    if stats:
        bits.append("  \u00b7  ".join(stats))
    tech = []
    if row.get("rsi14") is not None:
        tech.append(f"RSI {row['rsi14']:.0f}")
    if row.get("macd_bull"):
        tech.append("\U0001F7E2 MACD")
    if row.get("above_ema200"):
        tech.append("200DMA\u25b2")
    if row.get("sector"):
        tech.append(str(row["sector"]))
    if tech:
        bits.append("  \u00b7  ".join(tech))
    bits.append(f"  /fundamentalreport {escape(symbol)} for the deep report")
    return bits


def handle_screen(chat_id, parts) -> None:
    """Run the fundamental screener and reply with the top hits."""
    if len(parts) < 2:
        reply(chat_id, SCREEN_USAGE)
        return
    filters, sort, ascending, limit, universe = parse_screen_args(parts[1:])
    log.info("screen: universe=%s filters=%s sort=%s limit=%d (chat %s)",
             universe, filters, sort, limit, chat_id)
    reply(chat_id, f"\U0001F50D Screening <b>{universe.upper()}</b> \u2014 a moment…")
    try:
        rows = screen_universe(
            universe=universe, filters=filters, sort=sort,
            ascending=ascending, limit=limit, offset=0,
        )
    except Exception as error:
        log.warning("screen failed: %s", error)
        reply(chat_id, "\u26a0\ufe0f Screener is busy right now \u2014 try again in a minute.")
        return
    if not rows:
        reply(chat_id,
              "\U0001F50D No stocks match those filters \u2014 try loosening them "
              "(e.g. <code>/screen pe&lt;30 roe&gt;10</code>).")
        return
    lines = [f"\U0001F50D <b>SCREEN \u00b7 {universe.upper()}</b> \u2014 top {len(rows)}"]
    for index, row in enumerate(rows, 1):
        lines.append("")
        lines.extend(_format_row(index, row))
    lines.append("")
    lines.append("\U0001F4A1 <i>Tip: the same screen with presets lives on the web Screener page.</i>")
    reply_messages(chat_id, split_messages(lines))
