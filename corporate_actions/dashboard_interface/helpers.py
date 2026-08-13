"""Pure dashboard helpers - no Streamlit dependency.

Everything here is a plain function over plain data (watchlist items, quote
dicts, corporate-action dicts), so it is unit-testable and reusable from any
UI. The Streamlit-bound render helpers live in widgets.py and the per-tab
renderers in tabs/.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from .. import config, sources
from ..formatting.stock_india import _fund_report_lines, _stock_summary_lines
from ..market import MOVERS_PERIODS, fetch_period_change

# Table colour scheme for price/change cells (green up / red down / grey NA).
CELL_UP = "#16a34a"
CELL_DOWN = "#dc2626"
CELL_NA = "#9ca3af"


def label_of(item: dict) -> str:
    """Human label for a watchlist item, e.g. 'NSE · RELIANCE - Reliance Ind'."""
    company = f" - {item['company']}" if item.get("company") else ""
    code = f" [{item['code']}]" if item.get("code") else ""
    return f"{item['exchange']} · {item['symbol']}{code}{company}"


def item_from_label(label: str, stock_list: list[dict]) -> dict | None:
    """Reverse of label_of: find the stock-list item behind a label."""
    for item in stock_list:
        if label_of(item) == label:
            return item
    return None


def format_price(price) -> str:
    """₹ formatted price, '-' when missing."""
    try:
        return f"₹{float(price):,.2f}"
    except (TypeError, ValueError):
        return "-"


def format_change(change) -> str:
    """Signed % string; st.metric colours positive/negative deltas natively."""
    if change is None:
        return "-"
    try:
        change_value = float(change)
        sign = "+" if change_value >= 0 else ""
        return f"{sign}{change_value:.2f}%"
    except (TypeError, ValueError):
        return "-"


def style_table(rows: list[dict]) -> "pd.io.formats.style.Styler":
    """Return a pandas Styler that colours Price / Change % values inline.

    The green/red colour lives ON the value itself (modern, no extra emoji
    column): a row whose Change % is positive shows its Price and Change %
    in green, negative in red, and missing data in grey. Column headers
    stay sortable - the style follows the data when rows are re-ordered.
    """
    df = pd.DataFrame(rows)

    def _color_row(row):
        change = row.get("Change %")
        try:
            if change is None or pd.isna(change):
                return [f"color:{CELL_NA}"] * len(row)
            is_up = float(change) >= 0
        except (TypeError, ValueError):
            return [f"color:{CELL_NA}"] * len(row)
        color = CELL_UP if is_up else CELL_DOWN
        styles = [""] * len(row)
        for column in ("Price", "Change %"):
            if column in row.index:
                styles[row.index.get_loc(column)] = f"color:{color}; font-weight:600"
        return styles

    return df.style.apply(_color_row, axis=1)


def fetch_quotes_for(items: list[dict]) -> dict:
    """Fetch quotes for a list of watchlist items in parallel."""
    prices: dict = {}
    if not items:
        return prices
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(sources.get_quote, item["exchange"], item["symbol"]): (item["exchange"], item["symbol"])
            for item in items
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                prices[key] = future.result()
            except Exception:
                prices[key] = None
    return prices


def run_screen(period_key: str, direction: str, universe: str, count: int) -> list[dict]:
    """Run a market screen and return formatted rows (symbol, change, price, name)."""
    period = MOVERS_PERIODS.get(period_key, ("intraday", 60))
    exchange = sources.universe_exchange(universe)
    symbols = sources.get_index_universe(universe)
    if not symbols:
        return []

    def _fetch(symbol):
        return symbol, fetch_period_change(symbol, period, exchange=exchange)

    fetched = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(_fetch, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                data = future.result()[1]
            except Exception:
                data = None
            if data and data.get("change_pct") is not None:
                fetched.append((symbol, data))

    if direction == "gainers":
        fetched = [row for row in fetched if row[1]["change_pct"] > 0]
        fetched.sort(key=lambda row: row[1]["change_pct"], reverse=True)
    elif direction == "losers":
        fetched = [row for row in fetched if row[1]["change_pct"] < 0]
        fetched.sort(key=lambda row: row[1]["change_pct"])
    else:
        fetched.sort(key=lambda row: row[1]["change_pct"])

    # Raw numeric Price / Change % so the sortable table orders numerically;
    # the dataframe column_config applies the display formatting and the
    # Styler colours the values green/red inline.
    return [
        {
            "Symbol": symbol,
            "Price": data.get("price"),
            "Change %": data.get("change_pct"),
            "Name": data.get("name") or "",
        }
        for symbol, data in fetched[:count]
    ]


def tg_to_markdown(text: str) -> str:
    """Convert Telegram-HTML text into readable Streamlit markdown.

    Bold / italic / code / link tags become markdown equivalents, HTML
    entities are unescaped, and every line becomes a hard line break (two
    trailing spaces) so the compact block layout - symbol, company, subject,
    price and dates each on their own line - survives the web renderer
    instead of collapsing into one blob of raw tags.

    Price-change lines (the green/red arrow + percent) additionally get a
    real color span so they read colour-coded on the web; callers render
    this with unsafe_allow_html=True.
    """
    text = text or ""

    def _colorize(match):
        color = "#16a34a" if "\U0001F7E2" in match.group(1) else "#dc2626"
        return f'{match.group(1)}<span style="color:{color}">{match.group(2)} {match.group(3)}</span>'

    # "🟢▲ <b>+0.43%</b>" (and 🟡/🔴 variants) -> colored percent span.
    # The bold group must end in % so only price changes get colored, never
    # the bold symbol that follows the arrow in movers-style rows.
    text = re.sub(
        r"([\U0001F7E2\U0001F7E1\U0001F534])(\u25b2+|\u25bc+)\s*(<b>[^<]*%</b>)",
        _colorize, text,
    )
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.S)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.S)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.S)
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"[\2](\1)", text, flags=re.S)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"\n", "  \n", text)
    return text.strip()


def fetch_analysis(symbol: str) -> dict | None:
    """Fetch quote + deep fundamentals for a symbol (cross-link button).

    Tries NSE/BSE first (Indian screens), then US when the symbol only
    resolves on NASDAQ/NYSE so the market-screen cross-links keep working
    for the NASDAQ 100 universe.
    """
    quote = sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}
    if (quote.get("price") is None):
        us_quote = sources.get_quote("US", symbol)
        if us_quote and us_quote.get("price") is not None:
            return {
                "quote": us_quote,
                "fund": sources.get_us_fundamentals(symbol) or {},
                "sym": symbol,
                "us": True,
            }
    fund = sources.get_fundamentals(symbol, with_screener=True) or {}
    if not quote and not fund:
        return None
    return {"quote": quote, "fund": fund, "sym": symbol}


def md_escape(text: str) -> str:
    """Escape markdown-significant characters in raw data.

    Keeps titles/links from breaking the surrounding markdown (square
    brackets, parens, asterisks etc. would otherwise render as formatting
    or swallow the link). Also unescapes common HTML entities the feeds
    return.
    """
    text = (text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"([\\`*_\[\]()#!|])", r"\\\1", text)


def fetch_fund_lines(items: list[dict], deep: bool = True) -> list | str:
    """Fundamental report lines for every watchlist item (parallel).

    deep=True renders the DEEP report (like /fundamentalreport); deep=False
    renders the quick card (like /fundamentalanalyze). Returns a list of
    (symbol, markdown) pairs, or an error string.
    """

    def _one(item):
        symbol = item["symbol"]
        try:
            quote = sources.get_quote(item["exchange"], symbol) or {}
            fund = sources.get_fundamentals(symbol, with_screener=True) or {}
            if deep:
                lines = _fund_report_lines(symbol, quote, fund, include_tip=False)
            else:
                lines = _stock_summary_lines(symbol, quote, fund, include_tip=False)
            return symbol, tg_to_markdown("\n".join(lines))
        except Exception as error:
            return symbol, f"**{symbol}** — could not fetch fundamentals: {error}"

    if not items:
        return "Your watchlist is empty."
    results = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    # Keep the watchlist order
    order = {item["symbol"]: item for item in items}
    results.sort(key=lambda row: list(order).index(row[0]) if row[0] in order else 999)
    return results


def parse_telegram_watchlist(text: str) -> list[dict]:
    """Parse a Telegram watchlist message into a list of {'symbol', 'exchange'}.

    Handles the format produced by the bot's /watchlist command, e.g.:

        Your Watchlist:
        1. AMBER (NSE)
        2. ASHOKLEY (NSE)
        ...
        44. VBL (NSE)

        Use /fundamentalanalyze 5-10 or /fundamentalreport 3-5 to get details by these numbers.
        Saved in: subscriptions.json (your chat 862087765)
        Persistence: pushed to GitHub - it survives redeploys.

    Returns a list of dicts with 'symbol' and 'exchange' keys, preserving
    the order they appear in the pasted text.
    """
    items: list[dict] = []
    seen: set = set()
    # Match lines like "1. AMBER (NSE)" or "12. GOLDBEES (NSE)"
    pattern = re.compile(
        r"^\s*\d+[\.\)]\s*([A-Za-z0-9\-]+)\s*\(([A-Za-z0-9]+)\)\s*$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        line = line.strip()
        match = pattern.match(line)
        if not match:
            continue
        symbol = match.group(1).upper()
        exchange = match.group(2).upper()
        if exchange not in ("NSE", "BSE"):
            exchange = "NSE"
        key = (exchange, symbol)
        if key not in seen:
            seen.add(key)
            items.append({"symbol": symbol, "exchange": exchange})
    return items


def resolve_company_names(items: list[dict]) -> list[dict]:
    """Attach company names to watchlist items using the NSE stock list.

    Falls back to fetching a quote for symbols not found in the stock list.
    """
    if not items:
        return items

    # Build a lookup from the NSE stock list (cached).
    symbol_to_company: dict[str, str] = {}
    try:
        nse_list = sources.get_nse_stock_list_cached()
        for stock in nse_list:
            symbol_to_company[stock["symbol"].upper()] = stock.get("company", "")
    except Exception:
        nse_list = []

    resolved = []
    missing = []
    for item in items:
        company = symbol_to_company.get(item["symbol"].upper(), "")
        if company:
            resolved.append({**item, "company": company})
        else:
            missing.append(item)

    # For symbols not in the NSE list, try fetching a quote to get the name.
    if missing:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(sources.get_quote, item["exchange"], item["symbol"]): item
                for item in missing
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    quote = future.result()
                except Exception:
                    quote = None
                company = (quote or {}).get("name", "") if quote else ""
                resolved.append({**item, "company": company})

    return resolved
