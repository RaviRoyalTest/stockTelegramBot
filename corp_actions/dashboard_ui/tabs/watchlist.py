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
        options = [label_of(i) for i in stock_list]
        label_set = set(options)
        current = [label_of(i) for i in saved if label_of(i) in label_set]
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
        saved_keys = {(i["exchange"].upper(), i["symbol"].upper()) for i in saved}
        options_keys = {(i["exchange"].upper(), i["symbol"].upper()) for i in stock_list}
        extras = [i for i in saved if (i["exchange"].upper(), i["symbol"].upper()) not in options_keys]
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
        for i in current_watchlist:
            q = prices.get((i["exchange"], i["symbol"]))
            chg = q.get("change_pct") if q and q.get("change_pct") is not None else None
            rows.append({
                "Exchange": i["exchange"],
                "Symbol": i["symbol"],
                "Company": i.get("company", ""),
                "Price": q.get("price") if q and q.get("price") is not None else None,
                "Change %": chg,
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
        watch_syms = [r.get("Symbol", "") for r in rows]
        st.caption("Tap the 💹 button to open a stock's deep fundamentals report.")
        if watch_syms:
            per_row = 5
            for start in range(0, len(watch_syms), per_row):
                cols = st.columns(per_row)
                for j, sym in enumerate(watch_syms[start:start + per_row]):
                    with cols[j]:
                        symbol_fund_button(sym, f"watch_{start + j}", "watch")
        render_linked_analysis("watch")

        remove_options = [label_of(i) for i in current_watchlist]
        rm_col1, rm_col2 = st.columns([3, 1])
        with rm_col1:
            to_remove = st.multiselect("Remove stock(s)", options=remove_options, key="remove_select", default=[])
        with rm_col2:
            st.write("")
            if st.button("🗑️ Remove", width="stretch") and to_remove:
                for label in to_remove:
                    for i in current_watchlist:
                        if label_of(i) == label:
                            storage.remove_from_watchlist(i["symbol"], i["exchange"])
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
            fav = {}
            watch = storage.load_watchlist()
            try:
                matching = fetch_matching(watch)
                fav["corp_groups"] = {
                    "upcoming": [a for a in matching if within_reminder_window(a.get("ex_date"))],
                    "recent": [a for a in matching if recently_passed(a.get("ex_date"))],
                    "pending": [a for a in matching if not parse_ex_date(a.get("ex_date"))],
                }
                fav["corp"] = format_next_report(
                    fav["corp_groups"]["upcoming"],
                    fav["corp_groups"]["recent"],
                    fav["corp_groups"]["pending"],
                )
                for group in fav["corp_groups"].values():
                    attach_quotes(group)
            except Exception as exc:
                fav["corp"] = f"Could not fetch corporate actions: {exc}"
            fav["losers_1h"] = run_screen("1h", "losers", "nifty100", 10)
            fav["losers_today"] = run_screen("1d", "losers", "nifty100", 10)
            fav["fund"] = fetch_fund_lines(watch)
            st.session_state["favourites"] = fav

    fav = st.session_state.get("favourites")
    if fav:
        with st.expander("\U0001f4c5 Corporate actions for your list", expanded=True):
            if isinstance(fav.get("corp_groups"), dict):
                idx = 0
                for title, key in (("\U0001F4C5 Upcoming ex-dates", "upcoming"),
                                   ("\U0001F4E2 Announced - ex-date not fixed yet", "pending"),
                                   ("\U0001F504 Recently passed / in progress (past 30 days)", "recent")):
                    acts = sorted(fav["corp_groups"].get(key) or [],
                                  key=lambda a: a.get("ex_date") or "9999-99-99",
                                  reverse=(key == "recent"))
                    st.subheader(title)
                    for a in acts:
                        with st.container(border=True):
                            render_ca_card(a, f"favc_{idx}", "fav")
                        idx += 1
            else:
                st.markdown(tg_to_markdown(fav["corp"]), unsafe_allow_html=True)
        with st.expander("\U0001f4c9 Top losers — last 1h (NIFTY 100)"):
            st.caption("Values are colour-coded: green = up · red = down")
            st.dataframe(
                style_table(fav["losers_1h"]), width="stretch", hide_index=True,
                column_config={
                    "Price": st.column_config.NumberColumn("Price", format="₹%.2f"),
                    "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
                },
            )
            syms = [str(r.get("Symbol", "")) for r in fav["losers_1h"]]
            if syms:
                for start in range(0, len(syms), 5):
                    cols = st.columns(5)
                    for j, sym in enumerate(syms[start:start + 5]):
                        with cols[j]:
                            symbol_fund_button(sym, f"fav1h_{start + j}", "fav")
        with st.expander("\U0001f4c9 Top losers — today (NIFTY 100)"):
            st.caption("Values are colour-coded: green = up · red = down")
            st.dataframe(
                style_table(fav["losers_today"]), width="stretch", hide_index=True,
                column_config={
                    "Price": st.column_config.NumberColumn("Price", format="₹%.2f"),
                    "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
                },
            )
            syms = [str(r.get("Symbol", "")) for r in fav["losers_today"]]
            if syms:
                for start in range(0, len(syms), 5):
                    cols = st.columns(5)
                    for j, sym in enumerate(syms[start:start + 5]):
                        with cols[j]:
                            symbol_fund_button(sym, f"fav1d_{start + j}", "fav")
        st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
        render_linked_analysis("fav")
        with st.expander("\U0001f4ca Deep fundamentals — whole watchlist"):
            if isinstance(fav["fund"], list):
                for sym, lines in fav["fund"]:
                    st.markdown(lines, unsafe_allow_html=True)
                    st.divider()
            else:
                st.warning(fav["fund"])
