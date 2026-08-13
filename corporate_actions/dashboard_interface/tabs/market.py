"""📊 Market Screens tab - movers / gainers / losers / overnight gaps.

Gaps mirror the bot's /gappers: prev close -> open gaps, defaulting to
gap-downs. Every screen also accepts a specific date for that day's
historical movers / gaps.
"""
from __future__ import annotations

import datetime

import streamlit as st

from ...market import MOVERS_PERIODS, period_label
from ..helpers import run_screen, style_table
from ..widgets import render_linked_analysis, symbol_fund_button

_UNIVERSE_LABEL = {
    "nifty500": "NIFTY 500",
    "nasdaq100": "NASDAQ 100",
    "sp500": "S&P 500",
}.get


def render() -> None:
    st.header("📊 Market Screens")
    st.caption(
        "Screen NIFTY 100 / 500 / S&P 500 by price movement or overnight gaps. "
        "Pick a date for that day's historical movers / gaps."
    )

    column_1, column_2, column_3, column_4, column_5 = st.columns([1, 1, 1, 1, 1])
    with column_1:
        screen_type = st.selectbox(
            "Screen", ["Movers", "Gainers", "Losers", "Gaps"], key="screen_type"
        )
    with column_2:
        if screen_type == "Gaps":
            gap_direction = st.selectbox(
                "Direction", ["Gap Downs", "Gap Ups", "Both"], key="gap_direction"
            )
            period_key = "today"
        else:
            period_keys = list(MOVERS_PERIODS.keys())
            period_key = st.selectbox(
                "Period",
                period_keys,
                index=period_keys.index("1h"),
                key="screen_period",
            )
    with column_3:
        universe = st.selectbox(
            "Universe", ["nifty100", "nifty500", "nasdaq100", "sp500"],
            key="screen_universe",
        )
    with column_4:
        count = st.slider("Top N", 5, 50, 15, key="screen_count")
    with column_5:
        target_date = st.date_input(
            "Date (today by default)",
            value=datetime.date.today(),
            key="screen_date",
        )
        if target_date == datetime.date.today():
            target_date = None  # None = live/today

    if screen_type == "Gaps":
        direction = {"Gap Downs": "down", "Gap Ups": "up", "Both": "all"}[gap_direction]
        kind = "gap"
    else:
        direction = "all" if screen_type == "Movers" else screen_type.lower()
        kind = "movers"

    if st.button("🚀 Run screen", width="stretch"):
        with st.spinner(f"Fetching {screen_type.lower()} for {universe}..."):
            rows = run_screen(period_key, direction, universe, count,
                              kind=kind, target_date=target_date)
        st.session_state["screen_rows"] = rows
        st.session_state["screen_meta"] = (
            screen_type, period_key, direction, universe, count, target_date, kind,
        )

    if st.session_state.get("screen_rows"):
        screen_type, period_key, direction, universe, count, target_date, kind = \
            st.session_state["screen_meta"]
        universe_label = _UNIVERSE_LABEL(universe, "NIFTY 100")
        if target_date:
            window_text = f"on {target_date.strftime('%d-%b-%Y')}"
        elif kind == "gap":
            window_text = "today's gaps"
        else:
            window_text = period_label(*MOVERS_PERIODS.get(period_key, ("intraday", 60)))
        kind_text = f"{screen_type} ({'gap-downs' if direction == 'down' else 'gap-ups' if direction == 'up' else direction})" if screen_type == "Gaps" else screen_type
        st.subheader(f"{kind_text} — {window_text} · {universe_label} (top {count})")
        rows = st.session_state["screen_rows"]
        st.caption("Tap a column header to sort the table.  Values are colour-coded: green = up · red = down")
        price_format = "$%.2f" if universe in ("nasdaq100", "sp500") else "₹%.2f"
        column_config = {
            "Price": st.column_config.NumberColumn("Price", format=price_format),
            "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%"),
        }
        if kind == "gap":
            column_config.update({
                "Open": st.column_config.NumberColumn("Open", format=price_format),
                "Prev Close": st.column_config.NumberColumn("Prev Close", format=price_format),
                "Move %": st.column_config.NumberColumn("Move from open %", format="%+.2f%%"),
            })
        st.dataframe(
            style_table(rows),
            width="stretch",
            hide_index=True,
            column_config=column_config,
        )
        screen_syms = [str(row.get("Symbol", "")) for row in rows]
        st.caption("Tap the 💹 button to open a stock's deep fundamentals report.")
        if screen_syms:
            per_row = 5
            for start in range(0, len(screen_syms), per_row):
                cols = st.columns(per_row)
                for column_index, symbol in enumerate(screen_syms[start:start + per_row]):
                    with cols[column_index]:
                        symbol_fund_button(symbol, f"market_{start + column_index}", "screen")
        render_linked_analysis("screen")
