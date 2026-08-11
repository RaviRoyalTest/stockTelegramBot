"""Message-rendering package (Telegram HTML output for every report type)."""
from .actions import (
    action_status,
    format_action_block,
    format_action_detail,
    format_action_entry,
    format_corporate_action,
    format_mover_alert,
    format_next_report,
    format_price_alert,
    format_reminder,
    format_upcoming_list,
    status_tag,
)
from .news import format_news_item, format_news_list
from .schedule import format_interval, format_next_run, format_schedule, format_settings
from .stock import (
    _fundamentals_lines,
    _fund_report_lines,
    _rsi_signal,
    _stock_summary_lines,
    _wk52_signal,
)

__all__ = [
    "action_status",
    "format_action_block",
    "format_action_detail",
    "format_action_entry",
    "format_corporate_action",
    "format_next_report",
    "format_price_alert",
    "format_reminder",
    "format_upcoming_list",
    "status_tag",
    "format_mover_alert",
    "format_news_item",
    "format_news_list",
    "format_interval",
    "format_next_run",
    "format_schedule",
    "format_settings",
    "_fundamentals_lines",
    "_fund_report_lines",
    "_rsi_signal",
    "_stock_summary_lines",
    "_wk52_signal",
]
