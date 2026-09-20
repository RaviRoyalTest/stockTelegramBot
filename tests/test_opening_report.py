"""Unit tests for the opening/closing session screener.

Covers the pure logic: the US Mega/Large market-cap split (strict buckets,
never guessed), top-gainer/loser selection, the market-closed gate
(weekend + exchange holiday) and the batched Yahoo market-cap parser
including its 401 crumb-refresh retry. No network access.
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from corporate_actions.opening_report import data as d


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        return self._responses.pop(0)


class SplitUsByCapTests(unittest.TestCase):
    def test_strict_buckets_no_guessing(self):
        rows = [
            {"symbol": "MEGA", "market_cap": 300e9},
            {"symbol": "EXACT200", "market_cap": 200e9},  # boundary -> mega
            {"symbol": "LARGE", "market_cap": 199.9e9},
            {"symbol": "SMALL", "market_cap": 9.9e9},     # below $10B -> neither
        ]
        mega, large, unclassified = d.split_us_by_cap(rows, None)
        self.assertEqual([r["symbol"] for r in mega], ["MEGA", "EXACT200"])
        self.assertEqual([r["symbol"] for r in large], ["LARGE"])
        self.assertEqual(unclassified, 1)  # SMALL counted, never placed

    def test_unknown_cap_stays_unclassified(self):
        rows = [{"symbol": "NO cap"}, {"symbol": "LARGE", "market_cap": 50e9}]
        mega, large, unclassified = d.split_us_by_cap(rows, None)
        self.assertEqual((len(mega), len(large), unclassified), (0, 1, 1))

    def test_caps_dict_used_when_rows_lack_cap(self):
        rows = [{"symbol": "A"}, {"symbol": "B"}]
        mega, large, unclassified = d.split_us_by_cap(rows, {"A": 250e9, "B": 20e9})
        self.assertEqual((len(mega), len(large), unclassified), (1, 1, 0))


class TopMoverTests(unittest.TestCase):
    def test_gainers_sorted_desc_and_positive_only(self):
        rows = [
            {"change_pct": 1.0}, {"change_pct": 5.0},
            {"change_pct": -2.0}, {"change_pct": 0.0},
        ]
        self.assertEqual(
            [r["change_pct"] for r in d.top_gainers(rows)], [5.0, 1.0],
        )

    def test_losers_sorted_most_negative_first(self):
        rows = [{"change_pct": -1.0}, {"change_pct": -5.0}, {"change_pct": 3.0}]
        self.assertEqual(
            [r["change_pct"] for r in d.top_losers(rows)], [-5.0, -1.0],
        )

    def test_missing_change_pct_never_ranks(self):
        rows = [{"change_pct": None}, {"change_pct": 2.0}]
        self.assertEqual(len(d.top_gainers(rows)), 1)


class MarketStatusTests(unittest.TestCase):
    def test_weekend_is_closed(self):
        saturday = datetime(2026, 9, 19, 10, 0)  # a Saturday
        with patch("corporate_actions.opening_report.report.local_now", return_value=saturday):
            from corporate_actions.opening_report import report as r
            trading, reason = r._market_status("in")
        self.assertFalse(trading)
        self.assertIn("Weekend", reason)

    def test_exchange_holiday_detected_from_last_bar(self):
        from corporate_actions.opening_report import report as r
        monday = datetime(2026, 9, 21, 10, 0)
        with patch("corporate_actions.opening_report.report.local_now", return_value=monday), \
             patch.object(d, "latest_session_date", return_value=monday.date()):
            trading, reason = r._market_status("in")
        self.assertTrue(trading)

    def test_no_bars_today_means_holiday(self):
        from corporate_actions.opening_report import report as r
        monday = datetime(2026, 9, 21, 10, 0)
        last_friday = datetime(2026, 9, 18).date()  # a genuinely earlier session
        with patch("corporate_actions.opening_report.report.local_now", return_value=monday), \
             patch.object(d, "latest_session_date", return_value=last_friday):
            trading, reason = r._market_status("us")
        self.assertFalse(trading)
        self.assertIn("holiday", reason)


class GetUsMarketCapsTests(unittest.TestCase):
    """The caps come from Yahoo's batched v7/quote endpoint (chart meta has
    no marketCap - the previous implementation read a field that never
    exists and silently excluded every US stock)."""

    def _quote_payload(self, caps):
        return {"quoteResponse": {"result": [
            {"symbol": sym, "marketCap": cap} for sym, cap in caps.items()
        ]}}

    def test_batch_parsing_and_chunking(self):
        wanted = [f"S{i}" for i in range(120)]  # forces two chunks
        session = _FakeSession([
            _FakeResponse(payload=self._quote_payload({"S0": 1e11})),
            _FakeResponse(payload=self._quote_payload({"S100": 5e9})),
        ])
        with patch("corporate_actions.sources.fundamentals._fund_session",
                   return_value=(session, "crumb")):
            caps = d.get_us_market_caps(wanted)
        self.assertEqual(caps, {"S0": 1e11, "S100": 5e9})
        self.assertEqual(len(session.calls), 2)

    def test_401_refreshes_crumb_and_retries(self):
        session = _FakeSession([
            _FakeResponse(status_code=401),
            _FakeResponse(payload=self._quote_payload({"AAPL": 4.9e12})),
        ])
        with patch("corporate_actions.sources.fundamentals._fund_session",
                   return_value=(session, "crumb")), \
             patch("corporate_actions.sources.fundamentals._invalidate_crumb") as inval:
            caps = d.get_us_market_caps(["AAPL"])
        self.assertEqual(caps, {"AAPL": 4.9e12})
        inval.assert_called_once()

    def test_missing_cap_simply_absent(self):
        session = _FakeSession([
            _FakeResponse(payload={"quoteResponse": {"result": [
                {"symbol": "X", "marketCap": None},
                {"symbol": "Y", "marketCap": 2e10},
            ]}}),
        ])
        with patch("corporate_actions.sources.fundamentals._fund_session",
                   return_value=(session, "crumb")):
            caps = d.get_us_market_caps(["X", "Y"])
        self.assertEqual(caps, {"Y": 2e10})


class DailyPlanSanityTests(unittest.TestCase):
    def test_auto_plans_fire_exactly_at_their_clock_times(self):
        # Regression: a window_start/window_end pair makes the scheduler build
        # its own grid from the window edges and IGNORE run_at entirely.
        from corporate_actions.bot.opening_report_commands import _DAILY_PLANS
        from corporate_actions import scheduler as sched
        for plan in _DAILY_PLANS:
            self.assertNotIn("window_start", plan, plan)
            self.assertNotIn("window_end", plan, plan)
            entry = {
                "interval_min": 1440,
                "commands": [plan["command"]],
                "run_at": plan["run_at"],
                "market": plan["market"],
            }
            times = [t.strip() for t in plan["run_at"].split(",")]
            self.assertGreaterEqual(len(times), 2, plan)  # open AND close
            self.assertEqual(sched._entry_anchor_times(entry, 1440), times, plan)


if __name__ == "__main__":
    unittest.main()
