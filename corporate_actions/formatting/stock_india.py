"""Indian (NSE/BSE) stock-analysis report renderers (package facade).

The Indian renderers were split into single-purpose pure-function modules so
each stays small and independently testable:

  * stock_india_card.py   - compact quick card (_stock_summary_lines) + the
                            INR price-move line and shareholding rows it shares
                            with the DEEP report
  * stock_india_report.py - DEEP report (_fund_report_lines) incl. screener.in
                            annuals/quarters tables
  * stock_india_movers.py - compact movers-row lines (_fundamentals_lines)

Shared number/signal helpers (including the INR _cr_str) live in
stock_common.py; the US renderer lives in stock_us.py. This module only
re-exports the public API so existing import sites keep working.
"""
from __future__ import annotations

from .stock_common import _cr_str
from .stock_india_card import _holding_lines, _price_move_line, _stock_summary_lines
from .stock_india_movers import _fundamentals_lines
from .stock_india_report import (
    _annual_trend_lines,
    _arrow_pct,
    _cr_cr,
    _fund_report_lines,
    _quarterly_lines,
    _short_year,
)

__all__ = [
    "_fundamentals_lines",
    "_price_move_line",
    "_holding_lines",
    "_stock_summary_lines",
    "_cr_str",
    "_short_year",
    "_cr_cr",
    "_arrow_pct",
    "_annual_trend_lines",
    "_quarterly_lines",
    "_fund_report_lines",
]
