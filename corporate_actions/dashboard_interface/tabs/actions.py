"""📋 Corporate Actions tab - live NSE+BSE action queries with rich cards."""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from ... import config, sources, storage
from ...bot.helpers import attach_quotes
from ...formatting import format_next_report
from ...poller import (
    action_is_completed,
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
    symbol_picker,
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
            format_func=lambda type_name: sources.TYPE_LABELS.get(type_name, type_name),
        )
        if types:
            descriptor = {"mode": "types", "types": types}
    else:
        term = symbol_picker(
            "in",
            "Symbol or keyword (e.g. RELIANCE, TATA)",
            "ca_term",
            "Type to search NSE/BSE...",
            default_to_first=False,
        )
        if term:
            descriptor = {"mode": "term", "term": term}

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
                    except Exception as error:
                        st.error(f"Could not fetch corporate actions: {error}")
                        st.session_state["ca_fetched"] = False
                    else:
                        upcoming = [action for action in matching if within_reminder_window(action.get("ex_date"))]
                        recent = [action for action in matching if recently_passed(action.get("ex_date"))
                                  and not action_is_completed(action)]
                        pending = [action for action in matching if not parse_ex_date(action.get("ex_date"))]
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
                by_exchange, by_type = {}, {}
                for action in all_actions:
                    ex = action.get("exchange") or "?"
                    by_exchange[ex] = by_exchange.get(ex, 0) + 1
                    type_name = sources.action_type(action.get("subject"))
                    by_type[type_name] = by_type.get(type_name, 0) + 1
                dated = sorted(
                    (action for action in all_actions if parse_ex_date(action.get("ex_date"))),
                    key=lambda action: action.get("ex_date"),
                )[:15]
                st.session_state["ca_summary"] = {"by_ex": by_exchange, "by_type": by_type, "next": dated}
            elif mode == "exdate":
                today = date.today()
                cutoff = today + timedelta(days=descriptor["days"])
                results = [
                    action for action in all_actions
                    if (ex_date := parse_ex_date(action.get("ex_date"))) and today <= ex_date <= cutoff
                ]
            elif mode == "types":
                wanted = set(descriptor["types"])
                results = [action for action in all_actions if sources.action_type(action.get("subject")) in wanted]
            else:
                term = descriptor["term"].upper()
                results = [
                    action for action in all_actions
                    if (action.get("symbol") or "").upper() == term
                    or term.lower() in (action.get("company") or "").lower()
                    or term.lower() in (action.get("subject") or "").lower()
                ]
            results = sorted(results, key=lambda action: (action.get("ex_date") or "9999-99-99"))
            st.session_state["ca_results"] = results
            st.session_state["ca_fetched"] = True
            st.session_state["ca_errors"] = errors
            st.session_state["ca_warnings"] = warnings

    if st.session_state.get("ca_fetched"):
        errors = st.session_state.get("ca_errors", [])
        warnings = st.session_state.get("ca_warnings", [])
        if errors:
            for error in errors:
                st.error(error)
        if warnings:
            for warning in warnings:
                st.warning(warning)

        if st.session_state.get("ca_mylist"):
            groups = st.session_state.get("ca_mylist_groups")
            if groups:
                # Rich cards with a single-click 💹 button next to each symbol
                index = 0
                for title, key in (("\U0001F4C5 Upcoming ex-dates", "upcoming"),
                                   ("\U0001F4E2 Announced - ex-date not fixed yet", "pending"),
                                   ("\U0001F504 Recently passed / in progress (past 30 days)", "recent")):
                    acts = sorted(groups[key], key=lambda action: action.get("ex_date") or "9999-99-99",
                                  reverse=(key == "recent")) if groups[key] else []
                    st.subheader(title)
                    if not acts:
                        st.caption("None in this window.")
                    for action in acts:
                        with st.container(border=True):
                            render_ca_card(action, f"cam_{index}", "ca")
                        index += 1
                st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
                render_linked_analysis("ca")
            else:
                st.markdown(tg_to_markdown(st.session_state["ca_mylist"]), unsafe_allow_html=True)
        elif st.session_state.get("ca_summary"):
            summary = st.session_state["ca_summary"]
            summary_col_1, summary_col_2 = st.columns(2)
            by_exchange = summary["by_ex"]
            summary_col_1.markdown("**Count by exchange**  \n" + " · ".join(
                f"{exchange}: {count}" for exchange, count in by_exchange.items()
            ))
            by_type = summary["by_type"]
            summary_col_2.markdown("**Count by type**  \n" + (", ".join(
                f"{sources.TYPE_LABELS.get(type_name, type_name)} {by_type.get(type_name, 0)}"
                for type_name in sources.ACTION_TYPES if by_type.get(type_name)
            ) or "none"))
            st.subheader("Next ex-dates")
            with st.spinner("Fetching prices..."):
                quote_map = fetch_quotes_for(
                    [{"exchange": action["exchange"], "symbol": action["symbol"]} for action in summary["next"]]
                )
            for index, action in enumerate(summary["next"]):
                quote = quote_map.get((action["exchange"], action["symbol"]))
                if quote:
                    action["quote"] = quote
                with st.container(border=True):
                    render_ca_card(action, f"casum_{index}", "ca")
            st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
            render_linked_analysis("ca")
        else:
            results = st.session_state.get("ca_results", [])
            st.subheader(f"{len(results)} action(s) found")
            if results:
                # Add quotes for the first ~30
                with st.spinner("Fetching prices..."):
                    quote_map = fetch_quotes_for(
                        [{"exchange": action["exchange"], "symbol": action["symbol"]} for action in results[:30]]
                    )
                for index, action in enumerate(results):
                    if (action.get("quote") or {}).get("price") is None:
                        action["quote"] = quote_map.get((action["exchange"], action["symbol"])) or {}
                    with st.container(border=True):
                        render_ca_card(action, f"cares_{index}", "ca")
                st.caption("Tap the \U0001F4B9 button next to any symbol to open its deep fundamentals report.")
                render_linked_analysis("ca")
            else:
                st.info("No corporate actions match this query.")
