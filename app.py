"""Streamlit UI for selecting stocks and monitoring corporate-action alerts."""
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st

from corp_actions import config, notifier, sources, storage
from corp_actions.poller import poller

logging.basicConfig(level=logging.INFO, stream=sys.stdout)


def label_of(item: dict) -> str:
    company = f" - {item['company']}" if item.get("company") else ""
    code = f" [{item['code']}]" if item.get("code") else ""
    return f"{item['exchange']} · {item['symbol']}{code}{company}"


def item_from_label(label: str, stock_list: list[dict]) -> dict | None:
    for item in stock_list:
        if label_of(item) == label:
            return item
    return None


def persist_watchlist(selected_labels, stock_list):
    """Rebuild the watchlist from multiselect labels + previously saved extras."""
    selected = [
        item for label in selected_labels
        if (item := item_from_label(label, stock_list)) is not None
    ]
    saved = storage.load_watchlist()
    saved_keys = {(i["exchange"].upper(), i["symbol"].upper()) for i in saved}
    options_keys = {
        (i["exchange"].upper(), i["symbol"].upper()) for i in stock_list
    }
    extras = [
        i for i in saved
        if (i["exchange"].upper(), i["symbol"].upper()) not in options_keys
    ]
    return storage.save_watchlist(selected + extras)


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


@st.dialog("Check Results", width="large", dismissible=True)
def show_results_dialog(results: list[dict]):
    """Pop-up with all corporate actions available for the watchlist."""
    total = len(results)
    new_count = sum(1 for a in results if a.get("new"))
    st.caption(f"{total} corporate action(s) found for your watchlist "
               f"({new_count} new sent to Telegram).")
    if not results:
        st.info("No corporate actions available for your watchlist right now.")
    else:
        for a in results:
            q = a.get("quote")
            if q and q.get("price") is not None:
                price = q["price"]
                chg = q.get("change_pct")
                change = (f"{'+' if chg >= 0 else ''}{chg:.2f}%" if chg is not None else "-")
            else:
                price, change = None, "-"

            with st.container(border=True):
                st.markdown(f"### {a.get('symbol')} ({a.get('exchange')})")
                st.caption(a.get("company") or " ")
                st.markdown(f"**{a.get('subject')}**")

                m1, m2, m3 = st.columns(3)
                m1.metric("Ex-Date", a.get("ex_date") or "-")
                m2.metric("Record Date", a.get("record_date") or "-")
                if price is not None:
                    m3.metric("Price", f"{price:,.2f}", delta=change)
                else:
                    m3.metric("Price", "-")

                status = "New" if a.get("new") else "Already sent"
                st.write(f"Status: **{status}**")
    st.caption("Close this popup anytime and continue using the app.")
    if st.button("Close", type="primary"):
        st.rerun()


st.set_page_config(page_title="Corporate Action Alerts", layout="wide")
st.title("Corporate Action Alerts")
st.caption("Watch NSE & BSE corporate actions (dividends, splits, bonus, rights) "
           "and get them pushed to your Telegram bot.")

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Telegram")
    if notifier.is_configured():
        st.success(f"Configured · chat_id {config.TELEGRAM_CHAT_ID}")
    else:
        st.warning("Not configured. Copy `.env.example` to `.env` and set "
                   "`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.")
    if st.button("Send test message", disabled=not notifier.is_configured()):
        try:
            notifier.send_message("<b>Corporate Action Alerts</b> test message OK.")
            st.success("Test message sent.")
        except notifier.NotifierError as exc:
            st.error(str(exc))

    st.header("Sources")
    bse_default = st.session_state.get("enable_bse", config.ENABLE_BSE)
    enable_bse = st.toggle("Use BSE (usually blocked on datacenter IPs)",
                           value=bse_default)
    if enable_bse != config.ENABLE_BSE:
        config.ENABLE_BSE = enable_bse
        st.session_state["enable_bse"] = enable_bse

    st.header("Poller")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Start", disabled=poller.status["running"],
                     use_container_width=True):
            poller.start()
            st.rerun()
    with col2:
        if st.button("Stop", disabled=not poller.status["running"],
                     use_container_width=True):
            poller.stop()
            st.rerun()
    if st.button("Check now", use_container_width=True):
        try:
            poller.run_once()
            st.session_state["open_results"] = True
            st.success("Checked.")
        except Exception as exc:
            st.error(str(exc))
        st.rerun()

    st.header("Alert Settings")
    owner_key = str(config.TELEGRAM_CHAT_ID) or "local"
    ui_settings = storage.get_user_settings(owner_key)
    ui_filters = [
        f for f in (ui_settings.get("action_filters") or [])
        if f in sources.ACTION_TYPES
    ]
    sel_types = st.multiselect(
        "Only these action types (empty = all)",
        options=list(sources.ACTION_TYPES),
        default=ui_filters,
        format_func=lambda t: sources.TYPE_LABELS.get(t, t),
        key="action_type_filter",
    )
    stored_thresh = float(ui_settings.get("price_alert_pct") or 0.0)
    thresh = st.number_input(
        "Price-move alert threshold (%)",
        min_value=0.0,
        max_value=50.0,
        step=0.5,
        value=min(max(stored_thresh, 0.0), 50.0),
        key="price_alert_threshold",
    )
    if (
        (ui_settings.get("action_filters") or []) != sel_types
        or stored_thresh != thresh
    ):
        storage.save_user_settings(
            owner_key,
            {
                "action_filters": sel_types,
                "price_alert_pct": thresh if thresh > 0 else None,
            },
        )
    st.caption(f"Ex-date reminders: {config.REMINDER_DAYS} days ahead "
               f"(env REMINDER_DAYS, 0 disables).")

# --------------------------------------------------------------- status row
status = poller.status
st.subheader("Status")
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

# -------------------------------------------------------------- stock list
st.subheader("1. Load available stocks")
stock_list = st.session_state.get("stock_list", [])
if st.button("Load stock list from NSE & BSE", use_container_width=True):
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

if stock_list:
    st.caption(f"{len(stock_list)} stocks available.")

    # ----------------------------------------------------------- multiselect
    st.subheader("2. Select stocks to watch")
    saved = storage.load_watchlist()
    options = [label_of(i) for i in stock_list]
    label_set = set(options)
    current = [label_of(i) for i in saved if label_of(i) in label_set]
    current = list(dict.fromkeys(current))  # de-dup preserving order
    selected = st.multiselect(
        "Multi-select / type to filter. Deselect to remove.",
        options=options,
        default=current,
        key="watch_select",
        placeholder="Search symbols...",
    )
    persist_watchlist(selected, stock_list)

    # ------------------------------------------------------- paste watchlist
    st.subheader("2.5. Paste Telegram Watchlist")
    st.caption("Paste the full watchlist message from Telegram (e.g. from /watchlist) "
               "to replace the current watchlist in one go.")
    pasted_text = st.text_area(
        "Paste watchlist text here",
        height=150,
        key="paste_watchlist_app",
        placeholder=(
            "Your Watchlist:\n"
            "1. AMBER (NSE)\n"
            "2. ASHOKLEY (NSE)\n"
            "...\n"
            "44. VBL (NSE)\n"
        ),
    )
    if st.button("📥 Update watchlist from pasted text", use_container_width=True,
                 disabled=not pasted_text.strip()):
        parsed = parse_telegram_watchlist(pasted_text)
        if not parsed:
            st.error("No watchlist entries found in the pasted text. "
                     "Expected lines like `1. AMBER (NSE)`.")
        else:
            with st.spinner(f"Resolving {len(parsed)} symbols..."):
                resolved = resolve_company_names(parsed)
            storage.save_watchlist(resolved)
            st.success(f"Watchlist updated with {len(resolved)} stocks.")
            st.rerun()

    # ------------------------------------------------------- manual add/remove
    st.subheader("3. Adjust watchlist")
    add_col1, add_col2, add_col3 = st.columns([2, 1, 1])
    with add_col1:
        manual_symbol = st.text_input(
            "Symbol not in the list (e.g. RELIANCE, PGINVIT)",
            key="manual_symbol",
        )
    with add_col2:
        manual_exchange = st.selectbox("Exchange", ["NSE", "BSE"], key="manual_exchange")
    with add_col3:
        st.write("")
        if st.button("Add", use_container_width=True):
            sym = manual_symbol.strip().upper()
            if sym:
                quote = sources.get_quote(manual_exchange, sym)
                if quote is None:
                    st.error(f"Symbol {sym} not found. Check the ticker and exchange.")
                else:
                    company = quote.get("name", "")
                    storage.add_to_watchlist(
                        [{"symbol": sym, "company": company, "exchange": manual_exchange}]
                    )
                    st.success(f"Added {sym} ({company or manual_exchange}).")
                    st.rerun()
            else:
                st.error("Enter a symbol.")
    st.caption("InvITs/REITs (e.g. PGINVIT) are not in the NSE equities "
               "dropdown - add them here; they are validated against Yahoo.")

    current_watchlist = storage.load_watchlist()
    if current_watchlist:
        price_col1, price_col2 = st.columns([1, 4])
        with price_col1:
            refresh = st.button("Refresh prices", use_container_width=True)
        with price_col2:
            st.caption("Live prices via Yahoo Finance.")
        if refresh:
            with st.spinner("Fetching prices..."):
                prices = {}
                for item in current_watchlist:
                    q = sources.get_quote(item["exchange"], item["symbol"])
                    prices[(item["exchange"], item["symbol"])] = q
                st.session_state["prices"] = prices
            st.rerun()
        prices = st.session_state.get("prices", {})
        rows = []
        for i in current_watchlist:
            q = prices.get((i["exchange"], i["symbol"]))
            if q and q.get("price") is not None:
                change = q.get("change_pct")
                change_txt = f"{'+' if change is not None and change >= 0 else ''}{change:.2f}%" if change is not None else "-"
                price_txt = f"{q['price']:.2f} {q.get('currency', 'INR')}"
            else:
                price_txt, change_txt = "-", "-"
            rows.append({
                "Exchange": i["exchange"], "Symbol": i["symbol"],
                "Company": i.get("company", ""),
                "Price": price_txt, "Change %": change_txt,
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        remove_options = [label_of(i) for i in current_watchlist]
        rm_col1, rm_col2 = st.columns([3, 1])
        with rm_col1:
            to_remove = st.multiselect("Remove stock(s)", options=remove_options,
                                       key="remove_select", default=[])
        with rm_col2:
            st.write("")
            if st.button("Remove", use_container_width=True) and to_remove:
                for label in to_remove:
                    for i in current_watchlist:
                        if label_of(i) == label:
                            storage.remove_from_watchlist(i["symbol"], i["exchange"])
                st.success("Removed.")
                st.rerun()
    else:
        st.info("Watchlist is empty. Select stocks above or add a symbol manually.")

st.divider()
st.caption(f"Poll interval: {config.POLL_INTERVAL_SECONDS} s · "
           f"watchlist file: `{config.WATCHLIST_FILE}`")

if st.session_state.get("open_results"):
    st.session_state["open_results"] = False
    show_results_dialog(poller.status.get("last_results", []))