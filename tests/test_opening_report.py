"""Unit tests for the opening/closing session screener.

Covers the pure logic: the US Mega/Large market-cap split (strict buckets,
never guessed), top-gainer/loser selection, the market-closed gate
(weekend + exchange holiday) and the batched Yahoo market-cap parser
including its 401 crumb-refresh retry. No network access.
"""
import re
import unittest
from datetime import datetime
from unittest.mock import patch

from corporate_actions.opening_report import data as d
from corporate_actions.opening_report import tables as t


def _visible(line: str) -> str:
    """Strip HTML tags so length asserts measure what the phone actually wraps."""
    return re.sub(r"<[^>]+>", "", line)


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


class HistoricalSnapshotTests(unittest.TestCase):
    """Historical mode picks the bar ON the target date and the nearest
    earlier close as reference - Yahoo period responses can contain phantom
    null-close bars, which must never become the reference session."""

    @staticmethod
    def _epoch(y, m, d):
        import calendar

        return calendar.timegm((y, m, d, 12, 0, 0, 0, 0, 0))  # noon UTC

    def _quote(self):
        e15, e16, e17, e18 = (self._epoch(2026, 9, day) for day in (15, 16, 17, 18))
        timestamps = [e15, e16, e17, e18]
        quote = {
            "close": [10.0, 11.0, None, 12.0],   # 17-Sep is a phantom bar
            "volume": [100, 110, None, 240],
        }
        return timestamps, quote, e18

    def test_reference_skips_phantom_bar(self):
        from corporate_actions.opening_report import data as d

        timestamps, quote, end = self._quote()
        snap = d._historical_snapshot(quote, timestamps, end)
        self.assertEqual(snap, (12.0, 240, 11.0, 110))  # reference = 16-Sep

    def test_target_on_phantom_date_refused(self):
        from corporate_actions.opening_report import data as d

        timestamps, quote, _end = self._quote()
        phantom_day = self._epoch(2026, 9, 17)
        self.assertIsNone(d._historical_snapshot(quote, timestamps, phantom_day))

    def test_date_with_no_bar_refused(self):
        from corporate_actions.opening_report import data as d

        timestamps, quote, _end = self._quote()
        self.assertIsNone(d._historical_snapshot(quote, timestamps, self._epoch(2026, 9, 19)))

    def test_first_ever_bar_has_no_reference(self):
        from corporate_actions.opening_report import data as d

        e1, e2 = self._epoch(2026, 9, 15), self._epoch(2026, 9, 16)
        quote = {"close": [10.0, 11.0], "volume": [100, 110]}
        self.assertIsNone(d._historical_snapshot(quote, [e1, e2], e1))


class DailyPlanSanityTests(unittest.TestCase):
    def test_auto_install_writes_plans_without_window_keys(self):
        # Regression: /openreport auto crashed with KeyError 'window_start'
        # after the plans dropped their window keys (windows make the
        # scheduler ignore run_at). The installer must not reference them.
        from unittest.mock import MagicMock
        from corporate_actions.bot import opening_report_commands as orc
        # storage is imported inside the handler, so patch the package attr.
        with patch("corporate_actions.storage", new=MagicMock()) as mock_storage, \
                patch.object(orc, "reply") as mock_reply:
            orc.handle_openreport_auto(123, ["/openreport", "auto", "on"])
        self.assertTrue(mock_storage.add_schedule_entry.called)
        for call in mock_storage.add_schedule_entry.call_args_list:
            kwargs = call.kwargs
            self.assertNotIn("window_start", kwargs)
            self.assertNotIn("window_end", kwargs)
            self.assertIn("run_at", kwargs)
        plans = orc._DAILY_PLANS
        self.assertEqual(
            [c.kwargs["run_at"] for c in mock_storage.add_schedule_entry.call_args_list],
            [p["run_at"] for p in plans],
        )
        mock_reply.assert_called()  # user always gets a confirmation

    def test_auto_off_removes_entries_without_touching_others(self):
        from unittest.mock import MagicMock
        from corporate_actions.bot import opening_report_commands as orc
        entries = [
            {"commands": ["/openreport in"], "chat": "123"},
            {"commands": ["/toplosers 1h"], "chat": "123"},
        ]
        with patch("corporate_actions.storage", new=MagicMock()) as mock_storage, \
                patch.object(orc, "reply"):
            mock_storage.load_schedule_for.return_value = entries
            orc.handle_openreport_auto(123, ["/openreport", "auto", "off"])
        mock_storage.remove_schedule_entry.assert_called_once_with(123, 0)

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


class TelegramLayoutTests(unittest.TestCase):
    """The old fixed-width grid broke on long company names (numbers shifted
    sideways and 80-char lines wrapped mid-number). Pin the card layout."""

    ROWS = [
        {"symbol": "SOLARINDS", "name": "Solar Industries India",
         "price": 19890.0, "change": 625.0, "change_pct": 3.24,
         "volume": 396_600, "volume_change_pct": 84.05},
        {"symbol": "INDIGO", "name": "InterGlobe Aviation Limited",
         "price": 5030.0, "change": 90.0, "change_pct": 1.80,
         "volume": 873800, "volume_change_pct": None},  # missing prev-day volume
        {"symbol": "VBL", "name": "Varun Beverages Limited",
         "price": 430.0, "change": 5.0, "change_pct": 1.18,
         "volume": 5_140_000, "volume_change_pct": -12.4},
    ]

    def test_full_name_never_truncated(self):
        lines = t.table(self.ROWS, "INR")
        text = "\n".join(_visible(l) for l in lines)
        self.assertIn("InterGlobe Aviation Limited (INDIGO)", text)
        self.assertIn("Varun Beverages Limited (VBL)", text)
        self.assertNotIn("Limite", text.replace("Limited", ""))

    def test_name_line_width_capped(self):
        rows = [dict(self.ROWS[0], name="X" * 120)]
        lines = t.table(rows, "INR")
        for line in lines:
            if "XXX" in line:
                self.assertLessEqual(len(_visible(line)), 44)

    def test_data_line_short_and_ranked(self):
        lines = t.table(self.ROWS, "INR")
        data_lines = [_visible(l) for l in lines if "\u20b9" in l or "N/A" in l]
        for line in data_lines:
            self.assertLessEqual(len(line), 46, line)
            self.assertRegex(line, r"^\s*\d+\.")  # rank repeats on the metrics line

    def test_missing_volume_shows_na_token(self):
        lines = t.table(self.ROWS, "INR")
        text = "\n".join(_visible(l) for l in lines)
        self.assertIn("Vol N/A", text)
        self.assertIn("Vol \u25b284%", text)
        self.assertIn("Vol \u25bc12%", text)

    def test_negative_and_positive_render(self):
        lines = t.table(self.ROWS, "INR")
        text = "\n".join(_visible(l) for l in lines)
        self.assertIn("+3.24%", text)
        self.assertIn("+1.18%", text)  # signed pct on every metrics line

    def test_index_table_alignment(self):
        levels = [
            {"label": "Nifty 50", "level": 25506.0, "change": 85.1, "change_pct": 0.33},
            {"label": "Nifty Microcap 250", "level": None, "change": None, "change_pct": None},
        ]
        lines = t.index_table(levels)
        for line in lines:
            self.assertLessEqual(len(_visible(line)), 47)
        self.assertIn("N/A", "\n".join(lines))

    def test_empty_table_verified_zero(self):
        lines = t.table([], "INR")
        self.assertEqual(len(lines), 1)
        self.assertIn("Verified: 0/10", lines[0])

    def test_volume_lines_short(self):
        rows = [dict(self.ROWS[0])]
        buckets = t.volume_buckets_payload(rows)
        lines = t.volume_analysis_payload(buckets)
        for line in lines:
            self.assertLessEqual(len(_visible(line)), 46)


if __name__ == "__main__":
    unittest.main()
