"""📊 Market Screens tab - movers / gainers / losers over an index universe."""
from __future__ import annotations

import streamlit as st

from ...market import MOVERS_PERIODS, period_label
from ..helpers import run_screen, style_table
from ..widgets import render_linked_analysis, symbol_fund_button


def render() -> None:
    st.header("📊 Market Screens")
    st.caption("Screen NIFTY 100 / 500 by price movement over a time window.")

    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    with c1:
        screen_type = st.selectbox("Screen", ["Movers", "Gainers", "Losers"], key="screen_type")
    with c2:
        period_keys = list(MOVERS_PERIODS.keys())
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
            rows = run_screen(period_key, direction, universe, count)
        st.session_state["screen_rows"] = rows
        st.session_state["screen_meta"] = (screen_type, period_key, universe, len(rows))

    if st.session_state.get("screen_rows"):
        screen_type, period_key, universe, n = st.session_state["screen_meta"]
        period_label_text = period_label(*MOVERS_PERIODS.get(period_key, ("intraday", 60)))
        universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
        st.subheader(f"{screen_type} — {period_label_text} · {universe_label} (top {n})")
        rows = st.session_state["screen_rows"]
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
        screen_syms = [str(r.get("Symbol", "")) for r in rows]
        st.caption("Tap the 💹 button to open a stock's deep fundamentals report.")
        if screen_syms:
            per_row = 5
            for start in range(0, len(screen_syms), per_row):
                cols = st.columns(per_row)
                for j, sym in enumerate(screen_syms[start:start + per_row]):
                    with cols[j]:
                        symbol_fund_button(sym, f"market_{start + j}", "screen")
        render_linked_analysis("screen")
