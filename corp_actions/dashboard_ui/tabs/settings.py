"""🎛️ Alert Settings tab - filters, price threshold and the command reference."""
from __future__ import annotations

import streamlit as st

from ... import config, sources, storage
from ..help_text import HELP_TEXT


def render() -> None:
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
        # Merge into the existing settings so other keys (e.g. the sudden-move
        # watcher config saved via /watcher) are never wiped by this save.
        merged = dict(ui_settings)
        merged["action_filters"] = sel_types
        merged["price_alert_pct"] = thresh if thresh > 0 else None
        storage.save_user_settings(owner_key, merged)
        st.success("Settings saved.")

    st.divider()
    st.subheader("Telegram Command Reference")
    st.caption("All of these commands are also available directly in Telegram.")
    st.markdown(HELP_TEXT)
