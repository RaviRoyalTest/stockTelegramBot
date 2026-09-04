"""Stock screener tab for fundamentals + technical filters.

This page allows filtering a universe of stocks using a combined set of
fundamental and technical criteria. It uses the same public market data sources
already in the repository, with graceful fallback to Yahoo/market search if a
symbol is not immediately found.
"""
from __future__ import annotations

import io
import math
from typing import Any

import pandas as pd
try:
    import streamlit as st
except Exception:
    st = None

from ... import sources, storage


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def filter_candidates(rows: list[dict], filters: dict) -> list[dict]:
    """Apply combined fundamentals + technical rules to a list of stock rows."""
    out: list[dict] = []
    for row in rows:
        pe = _safe_float(row.get("pe"))
        roe = _safe_float(row.get("roe"))
        debt = _safe_float(row.get("debt_to_equity"))
        market_cap = _safe_float(row.get("market_cap"))
        rsi = _safe_float(row.get("rsi14"))
        macd_bull = bool(row.get("macd_bull"))
        above_ema200 = bool(row.get("above_ema200"))

        if filters.get("pe_max") is not None and (pe is None or pe > float(filters["pe_max"])):
            continue
        if filters.get("roe_min") is not None and (roe is None or roe < float(filters["roe_min"])):
            continue
        if filters.get("debt_to_equity_max") is not None and (debt is None or debt > float(filters["debt_to_equity_max"])):
            continue
        if filters.get("market_cap_min") is not None and (market_cap is None or market_cap < float(filters["market_cap_min"])):
            continue

        if filters.get("rsi_min") is not None and (rsi is None or rsi < float(filters["rsi_min"])):
            continue
        if filters.get("rsi_max") is not None and (rsi is None or rsi > float(filters["rsi_max"])):
            continue

        if filters.get("require_macd_bull") and not macd_bull:
            continue
        if filters.get("require_above_ema200") and not above_ema200:
            continue

        out.append(row)
    return out


def _candidate_rows_from_universe(universe: str) -> list[dict]:
    """Build candidate rows for the screener from the available universe and fundamentals data."""
    try:
        symbols = sources.get_index_universe(universe)
    except Exception:
        symbols = []
    if not symbols:
        symbols = [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "LTIM",
            "SBIN", "ITC", "SUNPHARMA", "AXISBANK", "BHARTIARTL",
            "WIPRO", "KOTAKBANK", "HINDUNILVR", "TATACONSUM",
        ]

    rows: list[dict] = []
    for symbol in symbols[:200]:
        try:
            fund = sources.get_fundamentals(symbol, with_screener=True) or {}
            quote = sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {}
            row = {
                "symbol": symbol,
                "company": fund.get("company") or fund.get("name") or symbol,
                "exchange": "NSE",
                "pe": _safe_float(fund.get("pe")),
                "roe": _safe_float(fund.get("roe")),
                "debt_to_equity": _safe_float(fund.get("debt_to_equity")),
                "market_cap": _safe_float(fund.get("market_cap")),
                "price": _safe_float(quote.get("price")),
                "change_pct": _safe_float(quote.get("change_pct")),
                "rsi14": _safe_float(fund.get("rsi14")),
                "macd_bull": bool(fund.get("macd_bull")),
                "above_ema200": bool(fund.get("above_ema200")),
            }
            if row["symbol"]:
                rows.append(row)
        except Exception:
            continue
    return rows


def _sort_rows(rows: list[dict], sort_key: str, ascending: bool) -> list[dict]:
    if not rows:
        return rows
    key_map = {
        "symbol": lambda r: (r.get("symbol") or "").upper(),
        "market_cap": lambda r: float(r.get("market_cap") or 0.0),
        "pe": lambda r: float(r.get("pe") or 999999),
        "roe": lambda r: float(r.get("roe") or -999999),
        "rsi": lambda r: float(r.get("rsi14") or 0.0),
        "change_pct": lambda r: float(r.get("change_pct") or 0.0),
        "price": lambda r: float(r.get("price") or 0.0),
    }
    return sorted(rows, key=key_map.get(sort_key, key_map["market_cap"]), reverse=not ascending)


def _build_chart_data(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["mcap_cr"] = (df["market_cap"].fillna(0) / 1_000_000_000).round(2)
    df["sector"] = df["company"].fillna("Unknown").map(lambda x: x.split()[0] if x else "Unknown")
    return df


def render() -> None:
    """Render the stock screener page with fundamentals + technical filters."""
    if st is None:
        raise RuntimeError("Streamlit UI removed; use the FastAPI templates instead.")
    st.markdown(
        """
        <style>
        .screener-hero { background: linear-gradient(135deg, #0f172a, #111827); padding: 1rem 1.2rem; border-radius: 18px; margin-bottom: 1rem; }
        .screener-pill { background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.2); border-radius: 999px; padding: 0.2rem 0.7rem; display: inline-block; margin-right: 0.4rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="screener-hero">
        <h2 style="margin:0;">📊 Stock Screener</h2>
        <div style="margin-top:0.5rem; color:#cbd5e1;">Premium scan for valuation, quality, and technical momentum.</div>
        <div style="margin-top:0.8rem;">
            <span class="screener-pill">Fundamentals</span>
            <span class="screener-pill">Technical</span>
            <span class="screener-pill">Watchlist flow</span>
            <span class="screener-pill">CSV export</span>
        </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    market = st.selectbox("Universe", ["nifty100", "nifty500", "india"], index=1)

    with st.sidebar:
        st.subheader("Fundamental filters")
        pe_max = st.number_input("P/E max", min_value=0.0, max_value=200.0, value=30.0, step=1.0)
        roe_min = st.number_input("ROE min %", min_value=0.0, max_value=50.0, value=8.0, step=1.0)
        debt_max = st.number_input("Debt/Equity max", min_value=0.0, max_value=10.0, value=1.5, step=0.1)
        market_cap_min = st.number_input("Min market cap (₹ cr)", min_value=0.0, max_value=5000000.0, value=500.0, step=50.0)

        st.subheader("Technical filters")
        rsi_min = st.number_input("RSI min", min_value=0, max_value=100, value=40, step=1)
        rsi_max = st.number_input("RSI max", min_value=0, max_value=100, value=75, step=1)
        require_macd_bull = st.checkbox("Only MACD bullish", value=True)
        require_above_ema200 = st.checkbox("Only above 200 EMA", value=True)

    filters = {
        "pe_max": pe_max,
        "roe_min": roe_min,
        "debt_to_equity_max": debt_max,
        "market_cap_min": market_cap_min,
        "rsi_min": rsi_min,
        "rsi_max": rsi_max,
        "require_macd_bull": require_macd_bull,
        "require_above_ema200": require_above_ema200,
    }

    if st.button("🔎 Run screener", type="primary", use_container_width=True):
        rows = _candidate_rows_from_universe(market)
        filtered = filter_candidates(rows, filters)
        st.session_state["screener_rows"] = filtered
        st.session_state["screener_filters"] = filters

    rows = st.session_state.get("screener_rows", [])
    if not rows:
        st.info("Run the screener to load matching stocks for your chosen fundamentals and technical criteria.")
        return

    sort_key = st.selectbox("Sort by", ["market_cap", "pe", "roe", "rsi", "change_pct", "price", "symbol"], index=0)
    ascending = st.checkbox("Ascending", value=False)
    sorted_rows = _sort_rows(rows, sort_key, ascending)

    summary_df = _build_chart_data(sorted_rows)
    metric_cols = st.columns(4)
    with metric_cols[0]:
        st.metric("Matches", len(sorted_rows))
    with metric_cols[1]:
        st.metric("Avg P/E", round(sum(float(r.get("pe") or 0.0) for r in sorted_rows) / max(len(sorted_rows), 1), 2))
    with metric_cols[2]:
        st.metric("Avg ROE %", round(sum(float(r.get("roe") or 0.0) for r in sorted_rows) / max(len(sorted_rows), 1), 2))
    with metric_cols[3]:
        st.metric("Avg MCap ₹Cr", round(sum(float(r.get("market_cap") or 0.0) for r in sorted_rows) / max(len(sorted_rows), 1), 2))

    st.subheader("Top setups")
    top_rows = sorted_rows[:5]
    top_cols = st.columns(len(top_rows) or 1)
    for idx, row in enumerate(top_rows):
        with top_cols[idx]:
            score = (
                (100.0 / max(float(row.get("pe") or 30.0), 1.0)) * 8.0
                + (float(row.get("roe") or 0.0) * 120.0)
                + (float(row.get("rsi14") or 50.0) * 0.25)
                + (10.0 if row.get("macd_bull") else 0.0)
                + (10.0 if row.get("above_ema200") else 0.0)
            )
            badge = min(99.9, max(0.0, score))
            st.markdown(f"<div style='border:1px solid rgba(148,163,184,0.3); border-radius:12px; padding:0.8rem; background:rgba(15,23,42,0.55);'>"
                        f"<h4 style='margin:0 0 0.3rem 0;'>{row.get('symbol')}</h4>"
                        f"<div style='color:#cbd5e1; font-size:0.85rem;'>{row.get('company') or '-'}</div>"
                        f"<div style='margin-top:0.5rem; font-weight:700;'>Score {badge:.1f}</div>"
                        f"<div style='margin-top:0.2rem;'>P/E {row.get('pe') if isinstance(row.get('pe'), (int, float)) else '-'} · RSI {row.get('rsi14') if isinstance(row.get('rsi14'), (int, float)) else '-'}</div>"
                        f"</div>", unsafe_allow_html=True)

    st.subheader("Charts")
    chart_cols = st.columns(2)
    if not summary_df.empty:
        sector_counts = summary_df["company"].fillna("Unknown").str.split().str[0].value_counts().head(8)
        final_mcap = pd.DataFrame({"Market cap ₹Cr": summary_df["mcap_cr"].fillna(0).clip(lower=0).sort_values(ascending=False).head(15)})
        with chart_cols[0]:
            st.bar_chart(sector_counts)
        with chart_cols[1]:
            st.bar_chart(final_mcap)

    selected_symbols = st.multiselect(
        "Select stocks to add to watchlist",
        options=[row["symbol"] for row in sorted_rows],
        default=[],
        key="screener_selected_symbols",
    )
    selected_rows = [row for row in sorted_rows if row.get("symbol") in selected_symbols]
    action_cols = st.columns([1, 1, 2])
    with action_cols[0]:
        if st.button("Add selected", type="primary", use_container_width=True, disabled=not selected_rows):
            payload = [{
                "symbol": row["symbol"],
                "company": row.get("company") or row["symbol"],
                "exchange": row.get("exchange") or "NSE",
            } for row in selected_rows]
            storage.add_to_watchlist(payload)
            st.success(f"Added {len(payload)} stock(s) to the watchlist.")
    with action_cols[1]:
        df = pd.DataFrame(sorted_rows)
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        st.download_button(
            label="Export CSV",
            data=csv_buffer.getvalue(),
            file_name="stock_screener_results.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.subheader(f"Results: {len(sorted_rows)}")
    for row in sorted_rows:
        with st.container(border=True):
            col1, col2, col3, col4, col5 = st.columns([2.2, 1.2, 1.2, 1.2, 1.2])
            with col1:
                st.markdown(f"### {row.get('symbol', '')}")
                st.caption(row.get("company") or "-")
            with col2:
                price = row.get("price")
                st.metric("Price", f"₹{price:,.2f}" if isinstance(price, (int, float)) else "-")
            with col3:
                change = row.get("change_pct")
                delta = f"{change:+.2f}%" if isinstance(change, (int, float)) else "-"
                st.metric("Change", delta)
            with col4:
                st.metric("P/E", f"{row.get('pe'):.2f}" if isinstance(row.get("pe"), (int, float)) else "-")
            with col5:
                st.metric("RSI", f"{row.get('rsi14'):.0f}" if isinstance(row.get("rsi14"), (int, float)) else "-")

            meta_cols = st.columns(5)
            meta_cols[0].write(f"ROE: {float(row.get('roe') or 0):.2f}%" if isinstance(row.get("roe"), (int, float)) else "ROE: -")
            meta_cols[1].write(f"D/E: {row.get('debt_to_equity'):.2f}" if isinstance(row.get("debt_to_equity"), (int, float)) else "D/E: -")
            meta_cols[2].write(f"MCap: ₹{float(row.get('market_cap') or 0):,.2f}Cr")
            meta_cols[3].write(f"MACD Bull: {'Yes' if row.get('macd_bull') else 'No'}")
            meta_cols[4].write(f"Above 200 EMA: {'Yes' if row.get('above_ema200') else 'No'}")

            if st.button("Add to watchlist", key=f"watchlist_{row.get('symbol')}", use_container_width=True):
                storage.add_to_watchlist([{
                    "symbol": row["symbol"],
                    "company": row.get("company") or row["symbol"],
                    "exchange": row.get("exchange") or "NSE",
                }])
                st.success(f"Added {row['symbol']} to the watchlist.")
