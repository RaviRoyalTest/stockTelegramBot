"""Stock-analysis report renderers (package facade).

The renderers were split by market so Indian and US output can never mix:

  * stock_common.py - shared number/signal helpers (_wk52_signal, _rsi_signal,
    _num_or_na, _pct_str, _growth_pct_str)
  * stock_india.py  - Indian (NSE/BSE) renderers: quick card, DEEP report and
    movers rows (INR, screener.in sections)
  * stock_us.py     - US renderer (_us_stock_lines, USD)

This module re-exports everything so existing import sites keep working.
"""
from __future__ import annotations

from .stock_common import _growth_pct_str, _num_or_na, _pct_str, _rsi_signal, _wk52_signal
from .stock_india import (
    _annual_trend_lines,
    _arrow_pct,
    _cr_cr,
    _cr_str,
    _fund_report_lines,
    _fundamentals_lines,
    _holding_lines,
    _price_move_line,
    _quarterly_lines,
    _short_year,
    _stock_summary_lines,
)
from .stock_us import _us_price_move_line, _us_stock_lines, _usd_compact

__all__ = [
    "_wk52_signal",
    "_rsi_signal",
    "_num_or_na",
    "_pct_str",
    "_growth_pct_str",
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
    "_usd_compact",
    "_us_price_move_line",
    "_us_stock_lines",
]
