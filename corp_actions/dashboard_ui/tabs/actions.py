"""📋 Corporate Actions tab - live NSE+BSE action queries with rich cards."""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from ... import config, sources, storage
from ...bot.helpers import attach_quotes
from ...formatting import format_next_report
from ...poller import (
    fetch_all_actions,
    fetch_matching,
    parse_ex_date,
    recently_passed,
    within_reminder_window,
)
from ..helpers import fetch_quotes_for, tg_to_markdown
from ..widgets import (
    render_ca_card,
    render_linked_analysis,
    symbol_fund_button,
)


def render() -> None:
    st.header("📋 Corporate Actions (NSE + BSE)")
    st.caption("Query live corporate actions — dividends, bonus, splits, rights, buybacks — "
               "including a dedicated view for YOUR watchlist.")

    q_mode = st.radio(
        "Query type",
        ["⭐ My List", "📊 Summary", "🗓️ By ex-date", "🔤 By type", "🔍 By symbol / keyword"],
        horizontal=True,
    )

    descriptor = None
    if q_mode == "⭐ My List":
        descriptor = {"mode": "mylist"}
    elif q_mode == "📊 Summary":
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
        mode = descriptor["mode"]
        st.session_state["ca_summary"] = None
        st.session_state["ca_mylist"] = None
        if mode == "mylist":
            # Corporate actions for the watchlist - mirrors /corpactionsformylist
            watch = storage.load_watchlist()
            if not watch:
                st.error("Your watchlist is empty. Add stocks in the 📌 Watchlist tab first.")
                st.session_state["ca_fetched"] = False
            else:
                with st.spinner("Fetching corporate actions for your list..."):
                    try:
                        matching = fetch_matching(watch)
                    except Exception as exc:
                        st.error(f"Could not fetch corporate actions: {exc}")
                        st.session_state["ca_fetched"] = False
                    else:
                        upcoming = [a for a in matching if within_reminder_window(a.get("ex_date"))]
                        recent = [a for a in matching if recently_passed(a.get("ex_date"))]
                        pending = [a for a in matching if not parse_ex_date(a.get("ex_date"))]
                        for group in (upcoming, recent, pending):
                            attach_quotes(group)
                        st.session_state["ca_mylist"] = format_next_report(upcoming, recent, pending)
                        st.session_state["ca_mylist_groups"] = {
                            "upcoming": upcoming, "recent": recent, "pending": pending,
                        }
                        st.session_state["ca_fetched"] = True
            st.session_state["ca_errors"] = []
            st.session_state["ca_warnings"] = []
        else:
            with st.spinner("Fetching corporate actions..."):
                all_actions, errors, warnings = fetch_all_actions()
            results = []
            if mode == "overview":
                # Mirror /corpactionssummary: counts by exchange & type + next ex-dates
                by_ex, by_type = {}, {}
                for a in all_actions:
                    ex = a.get("exchange") or "?"
                    by_ex[ex] = by_ex.get(ex, 0) + 1
                    t = sources.action_type(a.get("subject"))
                    by_type[t] = by_type.get(t, 0) + 1
                dated = sorted(
                    (a for a in all_actions if parse_ex_date(a.get("ex_date"))),
                    key=lambda a: a.get("ex_date"),
                )[:15]
                st.session_state["ca_summary"] = {"by_ex": by_ex, "by_type": by_type, "next": dated}
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

        if st.session_state.get("ca_mylist"):
            groups = st.session_state.get("ca_mylist_groups")
            if groups:
                # Rich cards with a single-click 💹 button next to each symbol
                idx = 0
                for title, key in (("\U0001F4C5 Upcoming ex-dates", "upcoming"),
                                   ("\U0001F4E2 Announced - ex-date not fixed yet", "pending"),
                                   ("\U0001F504 Recently passed / in progress (past 30 days)", "recent")):
                    acts = sorted(groups[key], key=lambda a: a.get("ex_date") or "9999-99-99",
                                  reverse=(key == "recent")) if groups[key] else []
                    st.subheader(title)
                    if not acts:
                        st.caption("None in this window.")
                    for a in acts:
                        with st.container(border=True):
                            render_ca_card(a, f"cam_{idx}", "ca")
                        idx += 1
                st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
                render_linked_analysis("ca")
            else:
                st.markdown(tg_to_markdown(st.session_state["ca_mylist"]), unsafe_allow_html=True)
        elif st.session_state.get("ca_summary"):
            summary = st.session_state["ca_summary"]
            s1, s2 = st.columns(2)
            by_ex = summary["by_ex"]
            s1.markdown("**Count by exchange**  \n" + " · ".join(
                f"{k}: {v}" for k, v in by_ex.items()
            ))
            by_type = summary["by_type"]
            s2.markdown("**Count by type**  \n" + (", ".join(
                f"{sources.TYPE_LABELS.get(t, t)} {by_type.get(t, 0)}"
                for t in sources.ACTION_TYPES if by_type.get(t)
            ) or "none"))
            st.subheader("Next ex-dates")
            with st.spinner("Fetching prices..."):
                quote_map = fetch_quotes_for(
                    [{"exchange": a["exchange"], "symbol": a["symbol"]} for a in summary["next"]]
                )
            for i, a in enumerate(summary["next"]):
                q = quote_map.get((a["exchange"], a["symbol"]))
                if q:
                    a["quote"] = q
                with st.container(border=True):
                    render_ca_card(a, f"casum_{i}", "ca")
            st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
            render_linked_analysis("ca")
        else:
            results = st.session_state.get("ca_results", [])
            st.subheader(f"{len(results)} action(s) found")
            if results:
                # Add quotes for the first ~30
                with st.spinner("Fetching prices..."):
                    quote_map = fetch_quotes_for(
                        [{"exchange": a["exchange"], "symbol": a["symbol"]} for a in results[:30]]
                    )
                for i, a in enumerate(results):
                    if (a.get("quote") or {}).get("price") is None:
                        a["quote"] = quote_map.get((a["exchange"], a["symbol"])) or {}
                    with st.container(border=True):
                        render_ca_card(a, f"cares_{i}", "ca")
                st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
                render_linked_analysis("ca")
            else:
                st.info("No corporate actions match this query.")
