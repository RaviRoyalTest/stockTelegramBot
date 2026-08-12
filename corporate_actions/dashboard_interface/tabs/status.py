"""🖥️ System Status tab - poller, watcher, schedule, subscribers, persistence."""
from __future__ import annotations

import os

import streamlit as st

from ... import config, scheduler, storage
from ...bot.schedule_commands import parse_interval_min
from ...formatting.schedule import format_next_run
from ...poller import poller


def render() -> None:
    st.header("🖥️ System Status")

    status = poller.status
    metric_col_1, metric_col_2, metric_col_3, metric_col_4 = st.columns(4)
    metric_col_1.metric("Poller running", "Yes" if status["running"] else "No")
    metric_col_2.metric("Last run", status["last_run"] or "never")
    metric_col_3.metric("Messages sent", status["total_sent"])
    metric_col_4.metric("Cycles", status["cycle"])
    if status["last_error"]:
        st.error(status["last_error"])
    if status.get("warnings"):
        for warn in status["warnings"]:
            st.warning(warn)
    if status["last_message"]:
        st.info(status["last_message"])

    # --- Force a check now (mirrors /checknow in Telegram)
    if st.button("⚡ Force check now (re-send all matching alerts)", width="stretch"):
        with st.spinner("Running a forced poll cycle..."):
            try:
                sent = poller.run_once(force=True)
                st.success(f"Check done — re-sent {sent} alert(s).")
            except Exception as error:
                st.error(f"Check failed: {config.redact(error)}")

    st.divider()
    st.subheader("\U0001F6A8 Sudden-move watcher")
    st.caption("Scans the chosen universe every few minutes and alerts this chat "
               "when any stock moves \u2265 the threshold % in a session "
               "(same as /watcher in Telegram - turn it on/off here anytime).")
    owner_key = str(config.TELEGRAM_CHAT_ID) or "local"
    ow_settings = storage.get_user_settings(owner_key)
    watcher = ow_settings.get("watcher") or {}
    w_enabled = bool(watcher.get("enabled"))
    w_thresh = float(watcher.get("threshold") or 5.0)
    w_universe = watcher.get("universe") or "nifty100"

    watcher_col_1, watcher_col_2, watcher_col_3 = st.columns([1, 1, 1])
    with watcher_col_1:
        w_enabled_new = st.toggle("Watcher ON", value=w_enabled, key="watcher_toggle")
    with watcher_col_2:
        w_thresh_new = st.number_input("Alert at \u2265 % move", min_value=0.5, max_value=50.0,
                                       step=0.5, value=min(max(w_thresh, 0.5), 50.0),
                                       key="watcher_threshold")
    with watcher_col_3:
        w_uni_new = st.selectbox("Universe", ["nifty100", "nifty500", "mylist"],
                                 index=["nifty100", "nifty500", "mylist"].index(w_universe)
                                 if w_universe in ("nifty100", "nifty500", "mylist") else 0,
                                 key="watcher_universe")
    if w_enabled_new != w_enabled or w_thresh_new != w_thresh or w_uni_new != w_universe:
        ow_settings["watcher"] = {
            "enabled": w_enabled_new,
            "threshold": float(w_thresh_new),
            "universe": w_uni_new,
        }
        storage.save_user_settings(owner_key, ow_settings)
        st.success("Watcher settings saved - "
                   + ("ON, alerts will arrive here." if w_enabled_new else "OFF."))
    st.caption(f"Scans every {config.MOVERS_WATCH_INTERVAL_SECONDS}s. "
               "Set the same thing on Telegram with /watcher.")

    st.divider()
    st.subheader("Automated reports (schedule)")
    st.caption("Every user manages their OWN schedule from Telegram with /schedule; "
               "this is the owner's view.")
    owner_key = str(config.TELEGRAM_CHAT_ID) or "local"
    sched_entries = storage.load_schedule_for(owner_key)
    if sched_entries:
        for index, entry in enumerate(sched_entries, start=1):
            interval = int(entry.get("interval_min") or 0)
            market = entry.get("market") or (storage.get_user_settings(owner_key) or {}).get(
                "schedule_market", config.SCHEDULED_REPORTS_MARKET
            )
            tz_tag = "ET" if market == "us" else "IST"
            tz_name = "America/New_York" if market == "us" else "Asia/Kolkata"
            if entry.get("run_at") and interval and interval % (24 * 60) == 0:
                label = "daily"
            else:
                label = f"every {interval} min"
                if interval and interval % (24 * 60) == 0:
                    label = f"every {interval // (24 * 60)}d"
                elif interval and interval % 60 == 0:
                    label = f"every {interval // 60}h"
            at_time = f" at {entry['run_at']} {tz_tag}" if entry.get("run_at") else ""
            due_ts = storage.schedule_next_due_ts(entry)
            next_run = f" — next run {format_next_run(due_ts, tz_name, tz_tag)}" if due_ts else ""
            st.markdown(f"**{index}.** {label}{at_time}: `{'`, `'.join(entry.get('commands') or [])}`{next_run}")
    else:
        commands = [command for command in config.SCHEDULED_COMMANDS if command.strip()]
        if commands:
            st.info("No file entries — env defaults run: "
                    f"every {config.SCHEDULED_REPORTS_INTERVAL_MIN} min → "
                    + ", ".join(commands))
        else:
            st.info("No automated reports scheduled yet.")

    stats_col_1, stats_col_2, stats_col_3, stats_col_4 = st.columns([2, 1, 1, 1])
    with stats_col_1:
        new_interval = st.text_input("Interval (minutes / 3h / 1d)", value="3h", key="sched_interval")
    ow_market = (storage.get_user_settings(owner_key) or {}).get(
        "schedule_market", config.SCHEDULED_REPORTS_MARKET
    )
    ow_tz_tag = "ET" if ow_market == "us" else "IST"
    with stats_col_2:
        new_at = st.text_input("At time (HH:MM, optional)", value="", key="sched_at",
                               placeholder=f"e.g. 09:15 {ow_tz_tag}")
    with stats_col_3:
        st.write("")
        if st.button("➕ Add", width="stretch", key="sched_add_btn"):
            interval = parse_interval_min(new_interval.strip())
            at_time = new_at.strip() or None
            if at_time and scheduler.next_at_ist(at_time) is None:
                st.error(f"Bad time. Use 24h format like 09:15 ({ow_tz_tag}).")
            elif interval is None:
                st.error("Bad interval. Use e.g. 180, 90m, 3h or 1d (min 15).")
            else:
                storage.add_schedule_entry(
                    interval, ["/scan500"], owner_key, run_at=at_time, market=ow_market
                )
                st.success(
                    f"Added /scan500 every {interval} min"
                    + (f" at {at_time} {ow_tz_tag}" if at_time else "") + "."
                )
                st.rerun()
    with stats_col_4:
        st.write("")
        if st.button("🗑️ Remove #1", width="stretch", key="sched_rm_btn") and sched_entries:
            storage.remove_schedule_entry(owner_key, 0)
            st.success("Removed entry 1.")
            st.rerun()
    st.caption("Add schedules `/scan500`; use Telegram `/schedule add 3h /scan500` or "
               "`/schedule add at 09:15 /cmd` (daily at a clock time) for any command. "
               "Remove deletes the first entry (use /schedule in Telegram for full "
               "control).")
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
                    [{"Exchange": item.get("exchange"), "Symbol": item.get("symbol"), "Company": item.get("company", "")}
                     for item in items],
                    width="stretch", hide_index=True,
                )
    else:
        st.info("No subscribers yet.")

    st.divider()
    st.subheader("Persistence (survives redeploys?)")
    github_available = bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))
    if github_available:
        st.success("GitHub push configured — state will survive redeploys.")
    else:
        st.warning("GH_TOKEN / GITHUB_REPOSITORY not set — state is only on this "
                   "host's disk and WILL BE LOST on redeploy. See README.")
