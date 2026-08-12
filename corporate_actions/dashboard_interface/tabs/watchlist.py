"""📌 Watchlist tab - manage the owner's watchlist and run My Favourites."""
from __future__ import annotations

import streamlit as st

from ... import config, sources, storage
from ...bot.helpers import attach_quotes
from ...formatting import format_next_report
from ...poller import (
    fetch_matching,
    parse_ex_date,
    recently_passed,
    within_reminder_window,
)
from ..helpers import (
    fetch_fund_lines,
    fetch_quotes_for,
    label_of,
    item_from_label,
    parse_telegram_watchlist,
    resolve_company_names,
    run_screen,
    style_table,
    tg_to_markdown,
)
from ..widgets import (
    render_ca_card,
    render_linked_analysis,
    symbol_fund_button,
)


def render() -> None:
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
                except sources.SourceError as error:
                    errors.append(f"{name}: {error}")
        st.session_state["stock_list"] = combined
        if errors:
            for error in errors:
                st.warning(error)
        if combined:
            st.success(f"Loaded {len(combined)} stocks.")
        st.rerun()

    saved = storage.load_watchlist()
    if stock_list:
        st.caption(f"{len(stock_list)} stocks available to pick from.")

        # Multiselect
        options = [label_of(item) for item in stock_list]
        label_set = set(options)
        current = [label_of(item) for item in saved if label_of(item) in label_set]
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
            if (item := item_from_label(label, stock_list)) is not None
        ]
        saved_keys = {(item["exchange"].upper(), item["symbol"].upper()) for item in saved}
        options_keys = {(item["exchange"].upper(), item["symbol"].upper()) for item in stock_list}
        extras = [item for item in saved if (item["exchange"].upper(), item["symbol"].upper()) not in options_keys]
        storage.save_watchlist(selected_items + extras)
    else:
        st.info("Load the stock list above to select stocks. You can also add "
                "symbols manually below.")

    # --- Paste Telegram watchlist
    st.subheader("📋 Paste Telegram Watchlist")
    st.caption("Paste the full watchlist message from Telegram (e.g. from /watchlist) "
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

    # --- Manual add / remove
    st.subheader("Add / Remove symbols")
    column_1, column_2, column_3 = st.columns([2, 1, 1])
    with column_1:
        manual_symbol = st.text_input("Symbol not in the list (e.g. RELIANCE, PGINVIT)", key="manual_symbol")
    with column_2:
        manual_exchange = st.selectbox("Exchange", ["NSE", "BSE"], key="manual_exchange")
    with column_3:
        st.write("")
        if st.button("➕ Add", width="stretch"):
            symbol = manual_symbol.strip().upper()
            if symbol:
                quote = sources.get_quote(manual_exchange, symbol)
                if quote is None:
                    # Mirror the bot's 'did you mean' flow: show close matches
                    # (ticker + company name) to pick from instead of a dead end.
                    st.session_state["wl_suggestions"] = sources.search_stocks(symbol, limit=6)
                    st.session_state["wl_query"] = symbol
                    st.session_state["wl_exchange"] = manual_exchange
                else:
                    st.session_state.pop("wl_suggestions", None)
                    company = quote.get("name", "")
                    storage.add_to_watchlist(
                        [{"symbol": symbol, "company": company, "exchange": manual_exchange}]
                    )
                    st.success(f"Added {symbol} ({company or manual_exchange}).")
                    st.rerun()
            else:
                st.error("Enter a symbol.")

    # --- Suggestion picker for a manual symbol that didn't resolve ---
    wl_suggestions = st.session_state.get("wl_suggestions")
    if wl_suggestions is not None:
        wl_query = st.session_state.get("wl_query", "")
        wl_exchange = st.session_state.get("wl_exchange", "NSE")
        if not wl_suggestions:
            st.error(
                f"Symbol {wl_query} not found on {wl_exchange}. Check the spelling "
                "or try a company name (e.g. RELIANCE)."
            )
        else:
            st.warning(f"{wl_query} not found on {wl_exchange}. Did you mean one of these?")
            wl_pick = st.selectbox(
                "Similar symbols",
                wl_suggestions,
                format_func=lambda match: f"{match['symbol']} — {match.get('company') or ''}",
                key="wl_pick",
            )
            if st.button("➕ Add selected", width="stretch"):
                storage.add_to_watchlist(
                    [{"symbol": wl_pick["symbol"], "company": wl_pick.get("company", ""),
                      "exchange": wl_exchange}]
                )
                st.session_state.pop("wl_suggestions", None)
                st.success(f"Added {wl_pick['symbol']} ({wl_exchange}).")
                st.rerun()

    # --- Current watchlist with prices
    current_watchlist = storage.load_watchlist()
    if current_watchlist:
        st.subheader(f"Current watchlist ({len(current_watchlist)} stocks)")
        if st.button("🔄 Refresh prices", width="stretch"):
            with st.spinner("Fetching prices..."):
                st.session_state["prices"] = fetch_quotes_for(current_watchlist)
            st.rerun()
        prices = st.session_state.get("prices")
        if prices is None:
            # Auto-load prices on first view so the table never shows a wall
            # of empty values; the Refresh button still re-fetches on demand.
            with st.spinner("Fetching prices..."):
                prices = fetch_quotes_for(current_watchlist)
                st.session_state["prices"] = prices
        rows = []
        for item in current_watchlist:
            quote = prices.get((item["exchange"], item["symbol"]))
            change = quote.get("change_pct") if quote and quote.get("change_pct") is not None else None
            rows.append({
                "Exchange": item["exchange"],
                "Symbol": item["symbol"],
                "Company": item.get("company", ""),
                "Price": quote.get("price") if quote and quote.get("price") is not None else None,
                "Change %": change,
            })
        # Sortable table (tap the column headers to sort) with one 💹 button
        # per row below - single click opens that stock's deep fundamentals.
        # Price & Change % are coloured inline by the Styler (green up / red down).
        st.caption("Tap a column header to sort the table.  Values are colour-coded: green = up · red = down")
        st.dataframe(
            style_table(rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Price": st.column_config.NumberColumn("Price", format="₹%.2f"),
                "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
            },
        )
        watch_syms = [row.get("Symbol", "") for row in rows]
        st.caption("Tap the 💹 button to open a stock's deep fundamentals report.")
        if watch_syms:
            per_row = 5
            for start in range(0, len(watch_syms), per_row):
                cols = st.columns(per_row)
                for column_index, symbol in enumerate(watch_syms[start:start + per_row]):
                    with cols[column_index]:
                        symbol_fund_button(symbol, f"watch_{start + column_index}", "watch")
        render_linked_analysis("watch")

        remove_options = [label_of(item) for item in current_watchlist]
        rm_col1, rm_col2 = st.columns([3, 1])
        with rm_col1:
            to_remove = st.multiselect("Remove stock(s)", options=remove_options, key="remove_select", default=[])
        with rm_col2:
            st.write("")
            if st.button("🗑️ Remove", width="stretch") and to_remove:
                for label in to_remove:
                    for item in current_watchlist:
                        if label_of(item) == label:
                            storage.remove_from_watchlist(item["symbol"], item["exchange"])
                st.success("Removed.")
                st.rerun()
    else:
        st.info("Watchlist is empty. Select stocks above or add a symbol manually.")

    # --- My Favourites bundle (mirrors /myfavourites in Telegram)
    st.divider()
    st.subheader("\u2b50 My Favourites (bundle)")
    st.caption("Runs your regular commands in one go — corporate actions for your "
               "list, top losers (last 1h + today), your watchlist, and deep "
               "fundamentals for every watchlist stock.")
    if st.button("\U0001f3c1 Run My Favourites", width="stretch"):
        with st.spinner("Running your favourites (corp actions, losers, fundamentals)..."):
            favourite = {}
            watch = storage.load_watchlist()
            try:
                matching = fetch_matching(watch)
                favourite["corp_groups"] = {
                    "upcoming": [action for action in matching if within_reminder_window(action.get("ex_date"))],
                    "recent": [action for action in matching if recently_passed(action.get("ex_date"))],
                    "pending": [action for action in matching if not parse_ex_date(action.get("ex_date"))],
                }
                favourite["corp"] = format_next_report(
                    favourite["corp_groups"]["upcoming"],
                    favourite["corp_groups"]["recent"],
                    favourite["corp_groups"]["pending"],
                )
                for group in favourite["corp_groups"].values():
                    attach_quotes(group)
            except Exception as error:
                favourite["corp"] = f"Could not fetch corporate actions: {error}"
            favourite["losers_1h"] = run_screen("1h", "losers", "nifty100", 10)
            favourite["losers_today"] = run_screen("1d", "losers", "nifty100", 10)
            favourite["fund"] = fetch_fund_lines(watch)
            st.session_state["favourites"] = favourite

    favourite = st.session_state.get("favourites")
    if favourite:
        with st.expander("\U0001f4c5 Corporate actions for your list", expanded=True):
            if isinstance(favourite.get("corp_groups"), dict):
                index = 0
                for title, key in (("\U0001F4C5 Upcoming ex-dates", "upcoming"),
                                   ("\U0001F4E2 Announced - ex-date not fixed yet", "pending"),
                                   ("\U0001F504 Recently passed / in progress (past 30 days)", "recent")):
                    acts = sorted(favourite["corp_groups"].get(key) or [],
                                  key=lambda action: action.get("ex_date") or "9999-99-99",
                                  reverse=(key == "recent"))
                    st.subheader(title)
                    for action in acts:
                        with st.container(border=True):
                            render_ca_card(action, f"favc_{index}", "fav")
                        index += 1
            else:
                st.markdown(tg_to_markdown(favourite["corp"]), unsafe_allow_html=True)
        with st.expander("\U0001f4c9 Top losers — last 1h (NIFTY 100)"):
            st.caption("Values are colour-coded: green = up · red = down")
            st.dataframe(
                style_table(favourite["losers_1h"]), width="stretch", hide_index=True,
                column_config={
                    "Price": st.column_config.NumberColumn("Price", format="₹%.2f"),
                    "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
                },
            )
            syms = [str(row.get("Symbol", "")) for row in favourite["losers_1h"]]
            if syms:
                for start in range(0, len(syms), 5):
                    cols = st.columns(5)
                    for column_index, symbol in enumerate(syms[start:start + 5]):
                        with cols[column_index]:
                            symbol_fund_button(symbol, f"fav1h_{start + column_index}", "fav")
        with st.expander("\U0001f4c9 Top losers — today (NIFTY 100)"):
            st.caption("Values are colour-coded: green = up · red = down")
            st.dataframe(
                style_table(favourite["losers_today"]), width="stretch", hide_index=True,
                column_config={
                    "Price": st.column_config.NumberColumn("Price", format="₹%.2f"),
                    "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
                },
            )
            syms = [str(row.get("Symbol", "")) for row in favourite["losers_today"]]
            if syms:
                for start in range(0, len(syms), 5):
                    cols = st.columns(5)
                    for column_index, symbol in enumerate(syms[start:start + 5]):
                        with cols[column_index]:
                            symbol_fund_button(symbol, f"fav1d_{start + column_index}", "fav")
        st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
        render_linked_analysis("fav")
        with st.expander("\U0001f4ca Deep fundamentals — whole watchlist"):
            if isinstance(favourite["fund"], list):
                for symbol, lines in favourite["fund"]:
                    st.markdown(lines, unsafe_allow_html=True)
                    st.divider()
            else:
                st.warning(favourite["fund"])
