"""Comprehensive web dashboard for the Stock Alert Bot.

Surfaces ALL bot functionality in a well-organized, customisable UI:
  * Watchlist management (owner + subscriber lists)
  * Live prices, market movers / gainers / losers screens
  * Corporate action queries (by type, ex-date, symbol, keyword)
  * Single-stock deep analysis
  * News
  * Alert settings (action-type filters + price threshold)
  * System status & persistence

Run locally:       streamlit run dashboard.py
Run on Render:     streamlit run dashboard.py --server.port $PORT
"""
from __future__ import annotations

import html
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import streamlit as st

import run_bot

from corp_actions import config, notifier, sources, storage
from corp_actions.poller import fetch_all_actions, parse_ex_date, poller

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# Show the bot's command help text rendered as a readable reference.
HELP_TEXT = (
    "\U0001F4CA **Stock Alert Bot — Command Guide**\n"
    "_Real-time NSE/BSE corporate actions, market movers & news_\n\n"
    "**Market Screens**\n"
    "- `/topmovers [period] [N] [100|500]` — top gainers AND losers in a window\n"
    "- `/topgainers [period] [N] [100|500]` — top rising stocks\n"
    "- `/toplosers [period] [N] [100|500]` — top falling stocks\n"
    "  Periods: 5m · 15m · 30m · 1h · 2h · 4h · 1d · 2d · 5d · 1w · 2w · 1mo · 3mo · 6mo · 1y\n"
    "  Universe: 100/nifty100=NIFTY 100 · 500/nifty500=NIFTY 500\n\n"
    "**Corporate Actions (NSE + BSE)**\n"
    "- `/corpactions [type|symbol|N|today]` — browse all dividend/bonus/split/rights/buyback actions\n"
    "- `/exdates [today|N]` — all actions by ex-date window\n"
    "- `/summary` — corporate-action snapshot: counts + next ex-dates\n"
    "- `/upcoming` — your watchlist: ex-dates + in-progress actions (rights/dividends) with status\n\n"
    "**Stock Analysis**\n"
    "- `/stockanalysis SYMBOL` — quick summary (price, P/E, 52W signal, QoQ holding)\n"
    "- `/fundamentals SYMBOL` — full deep-dive report (valuation, growth, margins, balance sheet)\n\n"
    "**Watchlist**\n"
    "- `/addstock SYMBOL [NSE|BSE]` · `/removestock SYMBOL` · `/watchlist`\n"
    "- `/news [N|SYMBOL]` — latest headlines\n\n"
    "**Alerts & Personalisation**\n"
    "- `/alertfilters TYPE,TYPE` — receive only chosen action types\n"
    "- `/pricealert PCT` — alert on ±PCT% daily move\n"
    "- `/settings` — view current config\n\n"
    "**System**\n"
    "- `/status` — where list is saved & GitHub push status\n"
    "- `/schedule add 3h /scan500` — run a command automatically\n"
    "- `/checknow` — force-run alerts\n"
    "- `/help` — this guide\n\n"
    "_Old short forms still work as aliases: /ca, /next, /add, /list, /movers, /gainers, /losers, /stock, /fund, /filter, /alert, /sched, /exdate._\n"
)

# Streamlit page config
st.set_page_config(
    page_title="Stock Alert Bot — Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------- helpers

def _label_of(item: dict) -> str:
    company = f" - {item['company']}" if item.get("company") else ""
    code = f" [{item['code']}]" if item.get("code") else ""
    return f"{item['exchange']} · {item['symbol']}{code}{company}"


def _item_from_label(label: str, stock_list: list[dict]) -> dict | None:
    for item in stock_list:
        if _label_of(item) == label:
            return item
    return None


def _fmt_price(price) -> str:
    try:
        return f"₹{float(price):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_change(change) -> str:
    if change is None:
        return "-"
    try:
        c = float(change)
        sign = "+" if c >= 0 else ""
        return f"{sign}{c:.2f}%"
    except (TypeError, ValueError):
        return "-"


def _change_color(change) -> str:
    try:
        return "🟢" if float(change) >= 0 else "🔴"
    except (TypeError, ValueError):
        return "⚪"


def _fetch_quotes_for(items: list[dict]) -> dict:
    """Fetch quotes for a list of watchlist items in parallel."""
    prices: dict = {}
    if not items:
        return prices
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {
            ex.submit(sources.get_quote, i["exchange"], i["symbol"]): (i["exchange"], i["symbol"])
            for i in items
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                prices[key] = fut.result()
            except Exception:
                prices[key] = None
    return prices


def _run_screen(period_key: str, direction: str, universe: str, count: int) -> list[dict]:
    """Run a market screen and return formatted rows (symbol, change, price, name)."""
    import run_bot
    period = run_bot.MOVERS_PERIODS.get(period_key, ("intraday", 60))
    symbols = sources.get_index_universe(universe)
    if not symbols:
        return []

    def _fetch(sym):
        return sym, run_bot._fetch_period_change(sym, period)

    fetched = []
    with ThreadPoolExecutor(max_workers=25) as ex:
        futures = {ex.submit(_fetch, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                data = fut.result()[1]
            except Exception:
                data = None
            if data and data.get("change_pct") is not None:
                fetched.append((sym, data))

    if direction == "gainers":
        fetched = [r for r in fetched if r[1]["change_pct"] > 0]
        fetched.sort(key=lambda r: r[1]["change_pct"], reverse=True)
    elif direction == "losers":
        fetched = [r for r in fetched if r[1]["change_pct"] < 0]
        fetched.sort(key=lambda r: r[1]["change_pct"])
    else:
        fetched.sort(key=lambda r: r[1]["change_pct"])

    return [
        {
            "Symbol": sym,
            "Price": _fmt_price(d.get("price")),
            "Change %": _fmt_change(d.get("change_pct")),
            "Name": d.get("name") or "",
        }
        for sym, d in fetched[:count]
    ]


def _parse_telegram_watchlist(text: str) -> list[dict]:
    """Parse a Telegram watchlist message into a list of {'symbol', 'exchange'}.

    Handles the format produced by the bot's /list command, e.g.:

        Your Watchlist:
        1. AMBER (NSE)
        2. ASHOKLEY (NSE)
        ...
        44. VBL (NSE)

        Use /stock 5-10 or /fund 3-5 to get details by these numbers.
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
        m = pattern.match(line)
        if not m:
            continue
        symbol = m.group(1).upper()
        exchange = m.group(2).upper()
        if exchange not in ("NSE", "BSE"):
            exchange = "NSE"
        key = (exchange, symbol)
        if key not in seen:
            seen.add(key)
            items.append({"symbol": symbol, "exchange": exchange})
    return items


def _resolve_company_names(items: list[dict]) -> list[dict]:
    """Attach company names to watchlist items using the NSE stock list.

    Falls back to fetching a quote for symbols not found in the stock list.
    """
    if not items:
        return items

    # Build a lookup from the NSE stock list (cached).
    symbol_to_company: dict[str, str] = {}
    try:
        nse_list = sources.get_nse_stock_list_cached()
        for s in nse_list:
            symbol_to_company[s["symbol"].upper()] = s.get("company", "")
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
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {
                ex.submit(sources.get_quote, i["exchange"], i["symbol"]): i
                for i in missing
            }
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    quote = fut.result()
                except Exception:
                    quote = None
                company = (quote or {}).get("name", "") if quote else ""
                resolved.append({**item, "company": company})

    return resolved


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📈 Stock Alert Bot")
    st.caption("NSE & BSE corporate actions, movers, news → Telegram")

    st.divider()
    st.subheader("🔌 Telegram Connection")
    if notifier.is_configured():
        st.success(f"Configured · chat_id `{config.TELEGRAM_CHAT_ID}`")
        if st.button("📨 Send test message", width="stretch"):
            try:
                notifier.send_message("<b>Stock Alert Bot</b> test message OK.")
                st.success("Test message sent.")
            except notifier.NotifierError as exc:
                st.error(str(exc))
    else:
        st.warning("Telegram not configured.")

    st.divider()
    st.subheader("⚙️ Poller")
    status = poller.status
    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶️ Start", disabled=status["running"], width="stretch"):
            poller.start()
            st.rerun()
    with col2:
        if st.button("⏹ Stop", disabled=not status["running"], width="stretch"):
            poller.stop()
            st.rerun()
    if st.button("🔍 Check now", width="stretch"):
        try:
            poller.run_once()
            st.success("Checked.")
        except Exception as exc:
            st.error(str(exc))
        st.rerun()

    st.divider()
    st.subheader("🌐 Sources")
    enable_bse = st.toggle(
        "Use BSE (usually WAF-blocked on datacenter IPs)",
        value=config.ENABLE_BSE,
    )
    if enable_bse != config.ENABLE_BSE:
        config.ENABLE_BSE = enable_bse
        st.rerun()

    st.divider()
    st.caption(f"Poll interval: {config.POLL_INTERVAL_SECONDS}s · "
               f"Reminder: {config.REMINDER_DAYS}d")

# ---------------------------------------------------------------- TABS
tab_watch, tab_actions, tab_market, tab_stock, tab_news, tab_settings, tab_status = st.tabs(
    [
        "📌 Watchlist",
        "📋 Corporate Actions",
        "📊 Market Screens",
        "💹 Stock Analysis",
        "📰 News",
        "🎛️ Alert Settings",
        "🖥️ System",
    ]
)

# ================================================================ WATCHLIST
with tab_watch:
    st.header("📌 Watchlist")
    st.caption("Manage the owner's app watchlist. Other Telegram subscribers "
               "have their own lists (see System tab).")

    # --- Load stock list
    stock_list = st.session_state.get("stock_list", [])
    if st.button("🔄 Load stock list from NSE & BSE", width="stretch"):
        with st.spinner("Fetching stock lists..."):
            combined, errors = [], []
            loaders = [("NSE", sources.get_nse_stock_list)]
            if config.ENABLE_BSE:
                loaders.append(("BSE", sources.get_bse_stock_list))
            for name, loader in loaders:
                try:
                    combined.extend(loader())
                except sources.SourceError as exc:
                    errors.append(f"{name}: {exc}")
        st.session_state["stock_list"] = combined
        if errors:
            for err in errors:
                st.warning(err)
        if combined:
            st.success(f"Loaded {len(combined)} stocks.")
        st.rerun()

    saved = storage.load_watchlist()
    if stock_list:
        st.caption(f"{len(stock_list)} stocks available to pick from.")

        # Multiselect
        options = [_label_of(i) for i in stock_list]
        label_set = set(options)
        current = [_label_of(i) for i in saved if _label_of(i) in label_set]
        current = list(dict.fromkeys(current))
        selected = st.multiselect(
            "Select stocks to watch (type to search)",
            options=options,
            default=current,
            key="watch_select",
            placeholder="Search symbols...",
        )
        # Persist
        selected_items = [
            item for label in selected
            if (item := _item_from_label(label, stock_list)) is not None
        ]
        saved_keys = {(i["exchange"].upper(), i["symbol"].upper()) for i in saved}
        options_keys = {(i["exchange"].upper(), i["symbol"].upper()) for i in stock_list}
        extras = [i for i in saved if (i["exchange"].upper(), i["symbol"].upper()) not in options_keys]
        storage.save_watchlist(selected_items + extras)
    else:
        st.info("Load the stock list above to select stocks. You can also add "
                "symbols manually below.")

    # --- Paste Telegram watchlist
    st.subheader("📋 Paste Telegram Watchlist")
    st.caption("Paste the full watchlist message from Telegram (e.g. from /list) "
               "to replace the current watchlist in one go.")
    pasted_text = st.text_area(
        "Paste watchlist text here",
        height=200,
        key="paste_watchlist",
        placeholder=(
            "Your Watchlist:\n"
            "1. AMBER (NSE)\n"
            "2. ASHOKLEY (NSE)\n"
            "3. ASKAUTOLTD (NSE)\n"
            "...\n"
            "44. VBL (NSE)\n"
        ),
    )
    if st.button("📥 Update watchlist from pasted text", width="stretch", disabled=not pasted_text.strip()):
        parsed = _parse_telegram_watchlist(pasted_text)
        if not parsed:
            st.error("No watchlist entries found in the pasted text. "
                     "Expected lines like `1. AMBER (NSE)`.")
        else:
            with st.spinner(f"Resolving {len(parsed)} symbols..."):
                resolved = _resolve_company_names(parsed)
            storage.save_watchlist(resolved)
            st.success(f"Watchlist updated with {len(resolved)} stocks.")
            st.rerun()

    # --- Manual add / remove
    st.subheader("Add / Remove symbols")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        manual_symbol = st.text_input("Symbol not in the list (e.g. RELIANCE, PGINVIT)", key="manual_symbol")
    with c2:
        manual_exchange = st.selectbox("Exchange", ["NSE", "BSE"], key="manual_exchange")
    with c3:
        st.write("")
        if st.button("➕ Add", width="stretch"):
            sym = manual_symbol.strip().upper()
            if sym:
                quote = sources.get_quote(manual_exchange, sym)
                if quote is None:
                    st.error(f"Symbol {sym} not found.")
                else:
                    company = quote.get("name", "")
                    storage.add_to_watchlist(
                        [{"symbol": sym, "company": company, "exchange": manual_exchange}]
                    )
                    st.success(f"Added {sym} ({company or manual_exchange}).")
                    st.rerun()
            else:
                st.error("Enter a symbol.")

    # --- Current watchlist with prices
    current_watchlist = storage.load_watchlist()
    if current_watchlist:
        st.subheader(f"Current watchlist ({len(current_watchlist)} stocks)")
        if st.button("🔄 Refresh prices", width="stretch"):
            with st.spinner("Fetching prices..."):
                st.session_state["prices"] = _fetch_quotes_for(current_watchlist)
            st.rerun()
        prices = st.session_state.get("prices", {})
        rows = []
        for i in current_watchlist:
            q = prices.get((i["exchange"], i["symbol"]))
            price = _fmt_price(q.get("price")) if q and q.get("price") is not None else "-"
            change = _fmt_change(q.get("change_pct")) if q else "-"
            color = _change_color(q.get("change_pct")) if q and q.get("change_pct") is not None else ""
            rows.append({
                "Exchange": i["exchange"],
                "Symbol": i["symbol"],
                "Company": i.get("company", ""),
                "Price": price,
                "Change %": f"{color} {change}",
            })
        st.dataframe(rows, width="stretch", hide_index=True)

        remove_options = [_label_of(i) for i in current_watchlist]
        rm_col1, rm_col2 = st.columns([3, 1])
        with rm_col1:
            to_remove = st.multiselect("Remove stock(s)", options=remove_options, key="remove_select", default=[])
        with rm_col2:
            st.write("")
            if st.button("🗑️ Remove", width="stretch") and to_remove:
                for label in to_remove:
                    for i in current_watchlist:
                        if _label_of(i) == label:
                            storage.remove_from_watchlist(i["symbol"], i["exchange"])
                st.success("Removed.")
                st.rerun()
    else:
        st.info("Watchlist is empty. Select stocks above or add a symbol manually.")

# ================================================================ CORPORATE ACTIONS
with tab_actions:
    st.header("📋 Corporate Actions (NSE + BSE)")
    st.caption("Query live corporate actions — dividends, bonus, splits, rights, buybacks.")

    q_mode = st.radio(
        "Query type",
        ["📊 Overview", "🗓️ By ex-date", "🔤 By type", "🔍 By symbol / keyword"],
        horizontal=True,
    )

    descriptor = None
    if q_mode == "📊 Overview":
        descriptor = {"mode": "overview"}
    elif q_mode == "🗓️ By ex-date":
        days = st.slider("Days ahead (0 = today)", 0, 30, config.REMINDER_DAYS)
        descriptor = {"mode": "exdate", "days": days}
    elif q_mode == "🔤 By type":
        types = st.multiselect(
            "Action types",
            options=list(sources.ACTION_TYPES),
            default=["dividend"],
            format_func=lambda t: sources.TYPE_LABELS.get(t, t),
        )
        if types:
            descriptor = {"mode": "types", "types": types}
    else:
        term = st.text_input("Symbol or keyword (e.g. RELIANCE, TATA)", key="ca_term")
        if term:
            descriptor = {"mode": "term", "term": term.strip()}

    if st.button("🔍 Run query", width="stretch", disabled=descriptor is None):
        with st.spinner("Fetching corporate actions..."):
            all_actions, errors, warnings = fetch_all_actions()
        mode = descriptor["mode"]
        results = []
        if mode == "overview":
            results = sorted(
                (a for a in all_actions if parse_ex_date(a.get("ex_date"))),
                key=lambda a: a.get("ex_date"),
            )[:50]
        elif mode == "exdate":
            today = date.today()
            cutoff = today + timedelta(days=descriptor["days"])
            results = [
                a for a in all_actions
                if (d := parse_ex_date(a.get("ex_date"))) and today <= d <= cutoff
            ]
        elif mode == "types":
            wanted = set(descriptor["types"])
            results = [a for a in all_actions if sources.action_type(a.get("subject")) in wanted]
        else:
            term = descriptor["term"].upper()
            results = [
                a for a in all_actions
                if (a.get("symbol") or "").upper() == term
                or term.lower() in (a.get("company") or "").lower()
                or term.lower() in (a.get("subject") or "").lower()
            ]
        results = sorted(results, key=lambda a: (a.get("ex_date") or "9999-99-99"))
        st.session_state["ca_results"] = results
        st.session_state["ca_fetched"] = True
        st.session_state["ca_errors"] = errors
        st.session_state["ca_warnings"] = warnings

    if st.session_state.get("ca_fetched"):
        errors = st.session_state.get("ca_errors", [])
        warnings = st.session_state.get("ca_warnings", [])
        if errors:
            for e in errors:
                st.error(e)
        if warnings:
            for w in warnings:
                st.warning(w)
        results = st.session_state.get("ca_results", [])
        st.subheader(f"{len(results)} action(s) found")
        if results:
            # Add quotes for the first ~30
            with st.spinner("Fetching prices..."):
                quote_map = _fetch_quotes_for(
                    [{"exchange": a["exchange"], "symbol": a["symbol"]} for a in results[:30]]
                )
            for a in results:
                q = quote_map.get((a["exchange"], a["symbol"]))
                price = _fmt_price(q.get("price")) if q and q.get("price") is not None else "-"
                change = _fmt_change(q.get("change_pct")) if q else "-"
                color = _change_color(q.get("change_pct")) if q and q.get("change_pct") is not None else ""
                typ = sources.action_type(a.get("subject"))
                with st.container(border=True):
                    st.markdown(f"### {a.get('symbol')} ({a.get('exchange')})")
                    st.caption(a.get("company") or "&nbsp;")
                    st.markdown(f"**{a.get('subject')}**")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Type", sources.TYPE_LABELS.get(typ, typ))
                    m2.metric("Ex-Date", a.get("ex_date") or "-")
                    m3.metric("Record Date", a.get("record_date") or "-")
                    m4.metric("Price", price, delta=change if change != "-" else None)
                    if a.get("announcement_date"):
                        st.caption(f"Announced: {a.get('announcement_date')} · "
                                   f"Face value: {a.get('face_value') or '-'} · "
                                   f"ISIN: {a.get('isin') or '-'}")
        else:
            st.info("No corporate actions match this query.")

# ================================================================ MARKET SCREENS
with tab_market:
    st.header("📊 Market Screens")
    st.caption("Screen NIFTY 100 / 500 by price movement over a time window.")

    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    with c1:
        screen_type = st.selectbox("Screen", ["Movers", "Gainers", "Losers"], key="screen_type")
    with c2:
        period_keys = list(run_bot.MOVERS_PERIODS.keys())
        period_key = st.selectbox(
            "Period",
            period_keys,
            index=period_keys.index("1h"),
            key="screen_period",
        )
    with c3:
        universe = st.selectbox("Universe", ["nifty100", "nifty500"], key="screen_universe")
    with c4:
        count = st.slider("Top N", 5, 50, 15, key="screen_count")

    direction = "all" if screen_type == "Movers" else screen_type.lower()
    if st.button("🚀 Run screen", width="stretch"):
        with st.spinner(f"Fetching {screen_type.lower()} for {universe}..."):
            rows = _run_screen(period_key, direction, universe, count)
        st.session_state["screen_rows"] = rows
        st.session_state["screen_meta"] = (screen_type, period_key, universe, len(rows))

    if st.session_state.get("screen_rows"):
        screen_type, period_key, universe, n = st.session_state["screen_meta"]
        period_label = run_bot._period_label(*run_bot.MOVERS_PERIODS.get(period_key, ("intraday", 60)))
        universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
        st.subheader(f"{screen_type} — {period_label} · {universe_label} (top {n})")
        st.dataframe(st.session_state["screen_rows"], width="stretch", hide_index=True)

# ================================================================ STOCK ANALYSIS
with tab_stock:
    st.header("💹 Single Stock Deep Analysis")
    st.caption("Full fundamentals, technicals, 52-week signal and QoQ holding for any NSE/BSE stock.")

    sym = st.text_input("Symbol (e.g. TATATECH, RELIANCE, INFY)", key="stock_sym").strip().upper()
    if st.button("🔍 Analyze", width="stretch", disabled=not sym):
        with st.spinner(f"Fetching analysis for {sym}..."):
            import run_bot
            quote = sources.get_quote("NSE", sym) or sources.get_quote("BSE", sym) or {}
            fund = sources.get_fundamentals(sym, with_screener=True) or {}

            if not quote and not fund:
                st.error(f"No data found for {sym}. Check the symbol.")
            else:
                st.session_state["stock_result"] = {"quote": quote, "fund": fund, "sym": sym}

    if st.session_state.get("stock_result"):
        res = st.session_state["stock_result"]
        quote, fund, sym = res["quote"], res["fund"], res["sym"]
        price = quote.get("price")
        change_pct = quote.get("change_pct")
        comp_name = quote.get("name") or sym

        st.subheader(f"{comp_name} ({sym})")
        if fund.get("sector"):
            st.caption(f"Sector: {fund['sector']}")

        # Price & today's movement
        col1, col2, col3 = st.columns(3)
        if price is not None:
            col1.metric("Current Price", _fmt_price(price),
                        delta=_fmt_change(change_pct) if change_pct is not None else None)
        else:
            col1.metric("Current Price", "-")
        col2.metric("52W High", _fmt_price(fund.get("wk52_high")) if fund.get("wk52_high") else "-")
        col3.metric("52W Low", _fmt_price(fund.get("wk52_low")) if fund.get("wk52_low") else "-")

        # 52-week signal
        if price and fund.get("wk52_high") and fund.get("wk52_low"):
            try:
                lo, hi = float(fund["wk52_low"]), float(fund["wk52_high"])
                spread = hi - lo
                if spread > 0:
                    pct_pos = (float(price) - lo) / spread
                    if pct_pos <= 0.15:
                        sig = "✅ Strong Buy — near 52-week LOW"
                    elif pct_pos <= 0.35:
                        sig = "📈 Buy Zone — low zone"
                    elif pct_pos >= 0.85:
                        sig = "🚫 Avoid — at/near 52-week HIGH"
                    elif pct_pos >= 0.65:
                        sig = "⚠️ High Zone — near 52-week HIGH"
                    else:
                        sig = "🟡 Mid-Range — middle of 52-week range"
                    st.info(sig)
            except (TypeError, ValueError):
                pass

        # RSI
        if fund.get("rsi") is not None:
            rsi = fund["rsi"]
            if rsi <= 30:
                rsi_txt = f"🟢 RSI {rsi} (Oversold)"
            elif rsi <= 45:
                rsi_txt = f"🟢 RSI {rsi} (Low)"
            elif rsi >= 70:
                rsi_txt = f"🔴 RSI {rsi} (Overbought)"
            elif rsi >= 60:
                rsi_txt = f"🔴 RSI {rsi} (High)"
            else:
                rsi_txt = f"🟡 RSI {rsi}"
            st.metric("RSI (14)", rsi_txt)

        # Fundamentals grid
        st.subheader("Valuation & Ratios")
        f1, f2, f3, f4, f5 = st.columns(5)
        f1.metric("P/E", f"{fund['pe']:.1f}" if fund.get("pe") else "N/A (Loss)")
        f2.metric("Sector P/E", f"{fund['sector_pe']:.1f}" if fund.get("sector_pe") else "-")
        f3.metric("Market Cap", f"₹{fund['market_cap']:,.0f}Cr" if fund.get("market_cap") else "-")
        f4.metric("D/E", f"{fund['debt_to_equity']:.2f}" if fund.get("debt_to_equity") else "-")
        f5.metric("Div Yield", f"{fund['div_yield']:.2f}%" if fund.get("div_yield") else "-")

        st.subheader("Profitability")
        p1, p2 = st.columns(2)
        p1.metric("ROCE", f"{fund['roce']:.1f}%" if fund.get("roce") else "-")
        p2.metric("ROE", f"{fund['roe']:.1f}%" if fund.get("roe") else "-")

        # Shareholding
        st.subheader("Shareholding Pattern (QoQ)")
        if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
            h1, h2, h3, h4 = st.columns(4)
            h1.metric("Promoter", fund.get("promoter_pct") or "-")
            h2.metric("FII", fund.get("fii_pct") or "-")
            h3.metric("DII", fund.get("dii_pct") or "-")
            h4.metric("Public", fund.get("public_pct") or "-")
        else:
            st.info("No shareholding breakdown available.")

        # Distance from 52 week
        if price and fund.get("wk52_high") and fund.get("wk52_low"):
            try:
                lo, hi = float(fund["wk52_low"]), float(fund["wk52_high"])
                dist_lo = ((float(price) - lo) / lo) * 100
                dist_hi = ((hi - float(price)) / hi) * 100
                st.caption(f"📍 +{dist_lo:.1f}% from 52w Low · -{dist_hi:.1f}% from 52w High")
            except (TypeError, ValueError, ZeroDivisionError):
                pass

# ================================================================ NEWS
with tab_news:
    st.header("📰 News")
    st.caption("Latest headlines for watchlist stocks or a single symbol.")

    c1, c2 = st.columns([2, 1])
    with c1:
        news_target = st.text_input("Symbol (leave empty for whole watchlist)", key="news_target").strip().upper()
    with c2:
        news_count = st.slider("Headlines per stock", 1, 5, 3, key="news_count")

    if st.button("📰 Fetch news", width="stretch"):
        with st.spinner("Fetching news..."):
            import run_bot
            if news_target:
                items = [{"symbol": news_target, "exchange": "NSE"}]
            else:
                items = storage.load_watchlist()
                if not items:
                    st.warning("Watchlist is empty — enter a symbol above.")
                    items = []
            results = []
            for item in items[:10]:
                news = sources.get_stock_news(item["exchange"], item["symbol"], news_count)
                results.append({"symbol": item["symbol"], "exchange": item["exchange"], "news": news})
            st.session_state["news_results"] = results

    if st.session_state.get("news_results"):
        for res in st.session_state["news_results"]:
            st.subheader(f"{res['symbol']} ({res['exchange']})")
            if not res["news"]:
                st.info("No recent news found.")
            for i, n in enumerate(res["news"], 1):
                title = n.get("title") or "-"
                link = n.get("link") or ""
                pub = n.get("published_ts")
                publisher = n.get("publisher") or ""
                meta = " | ".join(p for p in (publisher, notifier._fmt_ts(pub)) if p)
                if link:
                    st.markdown(f"{i}. [{title}]({link})")
                else:
                    st.markdown(f"{i}. {title}")
                if meta:
                    st.caption(meta)
            st.divider()

# ================================================================ ALERT SETTINGS
with tab_settings:
    st.header("🎛️ Alert Settings")
    st.caption("Customise which alerts you receive and how.")

    owner_key = str(config.TELEGRAM_CHAT_ID) or "local"
    ui_settings = storage.get_user_settings(owner_key)
    ui_filters = [f for f in (ui_settings.get("action_filters") or []) if f in sources.ACTION_TYPES]

    st.subheader("Action-type filters")
    st.caption("Only receive these corporate-action types. Empty = all types.")
    sel_types = st.multiselect(
        "Only these action types (empty = all)",
        options=list(sources.ACTION_TYPES),
        default=ui_filters,
        format_func=lambda t: sources.TYPE_LABELS.get(t, t),
        key="action_type_filter",
    )

    st.subheader("Price-move alert")
    stored_thresh = float(ui_settings.get("price_alert_pct") or 0.0)
    thresh = st.number_input(
        "Alert when a watched stock moves ±X% in a day (0 = off)",
        min_value=0.0, max_value=50.0, step=0.5,
        value=min(max(stored_thresh, 0.0), 50.0),
        key="price_alert_threshold",
    )

    if (ui_settings.get("action_filters") or []) != sel_types or stored_thresh != thresh:
        storage.save_user_settings(owner_key, {
            "action_filters": sel_types,
            "price_alert_pct": thresh if thresh > 0 else None,
        })
        st.success("Settings saved.")

    st.divider()
    st.subheader("Telegram Command Reference")
    st.caption("All of these commands are also available directly in Telegram.")
    st.markdown(HELP_TEXT)

# ================================================================ SYSTEM
with tab_status:
    st.header("🖥️ System Status")

    status = poller.status
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Poller running", "Yes" if status["running"] else "No")
    m2.metric("Last run", status["last_run"] or "never")
    m3.metric("Messages sent", status["total_sent"])
    m4.metric("Cycles", status["cycle"])
    if status["last_error"]:
        st.error(status["last_error"])
    if status.get("warnings"):
        for warn in status["warnings"]:
            st.warning(warn)
    if status["last_message"]:
        st.info(status["last_message"])

    st.divider()
    st.subheader("Configuration")
    cfg_cols = st.columns(3)
    cfg_cols[0].write(f"**Poll interval:** {config.POLL_INTERVAL_SECONDS}s")
    cfg_cols[1].write(f"**Reminder days:** {config.REMINDER_DAYS}")
    cfg_cols[2].write(f"**BSE enabled:** {config.ENABLE_BSE}")
    cfg_cols = st.columns(3)
    cfg_cols[0].write(f"**Watchlist file:** `{config.WATCHLIST_FILE.name}`")
    cfg_cols[1].write(f"**Settings file:** `{config.SETTINGS_FILE.name}`")
    cfg_cols[2].write(f"**Seen cache:** `{config.SEEN_FILE.name}`")

    st.divider()
    st.subheader("Subscribers")
    subs = storage.load_subscriptions()
    if subs:
        for chat_id, items in subs.items():
            with st.expander(f"Chat {chat_id} — {len(items)} stocks"):
                st.dataframe(
                    [{"Exchange": i.get("exchange"), "Symbol": i.get("symbol"), "Company": i.get("company", "")}
                     for i in items],
                    width="stretch", hide_index=True,
                )
    else:
        st.info("No subscribers yet.")

    st.divider()
    st.subheader("Persistence (survives redeploys?)")
    import os
    gh = bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))
    if gh:
        st.success("GitHub push configured — state will survive redeploys.")
    else:
        st.warning("GH_TOKEN / GITHUB_REPOSITORY not set — state is only on this "
                   "host's disk and WILL BE LOST on redeploy. See README.")