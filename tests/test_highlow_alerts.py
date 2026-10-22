"""52-week high/low alert tests: detection, dedup, rendering and the scan.

The feature alerts only on a FRESH extreme - today's session must clear the
highest high (or lowest low) of the previous 52 weeks - so a stock parked at
a high set weeks ago stays quiet instead of pinging every cycle.
"""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from corporate_actions.core.dates import IST
from corporate_actions.formatting.actions import format_52w_alert
from corporate_actions.poller import highlow
from corporate_actions.poller.engine import Poller

TODAY = date(2026, 10, 22)


def _ts(day: date) -> float:
    """Epoch seconds of a session bar opened 09:15 IST on ``day``."""
    moment = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    return moment.timestamp()


def _bars(rows) -> dict:
    """Yahoo-shaped daily bars from [(days_ago, high, low), ...]."""
    return {
        "timestamp": [_ts(TODAY - timedelta(days=ago)) for ago, _, _ in rows],
        "high": [high for _, high, _ in rows],
        "low": [low for _, _, low in rows],
        "open": [low for _, _, low in rows],
        "close": [high for _, high, _ in rows],
        "volume": [1000 for _ in rows],
    }


QUOTE_HIGH = {
    "price": 1912.0,
    "prev_close": 1860.0,
    "change_pct": 2.8,
    "high": 1912.0,
    "low": 1861.0,
    "wk52_high": 1912.0,
    "wk52_low": 1200.0,
    "pe": 24.5,
    "currency": "INR",
    "name": "Infosys Limited",
}
DETAILS = {
    "pe": 42.3,
    "sector_pe": 59.4,
    "debt_to_equity": 0.27,
    "price_to_book": 21.8,
    "div_yield": 1.96,
    "mcap_cr": 111401.6,
    "roe": 60.1,
    "roce": 61.4,
    "rsi": 33.2,
    "book_value": 212.006,
    "trailing_eps": 108.11,
    "profit_margin": 0.13376,
    "sector": "Fast Moving Consumer Goods",
    "industry": "Packaged Foods",
}
QUOTE_LOW = {
    "price": 1140.0,
    "prev_close": 1180.0,
    "change_pct": -3.4,
    "high": 1181.0,
    "low": 1139.0,
    "wk52_high": 1611.8,
    "wk52_low": 1139.0,
    "currency": "INR",
    "name": "Reliance Industries Limited",
}


class CandidateTests(unittest.TestCase):
    def test_session_high_at_the_52w_level_is_a_candidate(self):
        self.assertEqual(highlow.candidate_kind(QUOTE_HIGH), "high")

    def test_session_low_at_the_52w_level_is_a_candidate(self):
        self.assertEqual(highlow.candidate_kind(QUOTE_LOW), "low")

    def test_mid_range_stock_is_not_a_candidate(self):
        quote = dict(QUOTE_HIGH, high=1700.0, low=1600.0, price=1650.0)
        self.assertIsNone(highlow.candidate_kind(quote))

    def test_missing_levels_never_crash(self):
        self.assertIsNone(highlow.candidate_kind({"price": 100.0}))
        self.assertIsNone(highlow.candidate_kind({}))
        self.assertIsNone(highlow.candidate_kind(None))


class FreshExtremeTests(unittest.TestCase):
    def test_new_high_above_prior_year_is_fresh(self):
        bars = _bars([(1, 1900.0, 1850.0), (5, 1890.0, 1800.0), (200, 1880.0, 1700.0)])
        self.assertTrue(highlow.is_fresh_extreme("NSE", "INFY", "high", QUOTE_HIGH, TODAY, bars))

    def test_parked_at_a_prior_high_is_not_fresh(self):
        # Today's 1912 merely matches the 1912 high set two sessions ago.
        bars = _bars([(2, 1912.0, 1800.0), (30, 1900.0, 1700.0)])
        self.assertFalse(highlow.is_fresh_extreme("NSE", "INFY", "high", QUOTE_HIGH, TODAY, bars))

    def test_new_low_below_prior_year_is_fresh(self):
        bars = _bars([(1, 1200.0, 1150.0), (100, 1400.0, 1160.0)])
        self.assertTrue(highlow.is_fresh_extreme("NSE", "RELIANCE", "low", QUOTE_LOW, TODAY, bars))

    def test_low_above_the_prior_year_low_is_not_fresh(self):
        bars = _bars([(1, 1200.0, 1139.0)])
        self.assertFalse(highlow.is_fresh_extreme("NSE", "RELIANCE", "low", QUOTE_LOW, TODAY, bars))

    def test_todays_own_bar_never_counts_as_prior(self):
        # The bar dated today carries the very high being judged; if it were
        # included every candidate would look like a retest.
        bars = _bars([(0, 1950.0, 1130.0), (4, 1900.0, 1150.0)])
        self.assertTrue(highlow.is_fresh_extreme("NSE", "INFY", "high", QUOTE_HIGH, TODAY, bars))

    def test_bars_older_than_the_lookback_are_ignored(self):
        bars = _bars([(10, 1700.0, 1300.0), (600, 3000.0, 500.0)])
        self.assertTrue(highlow.is_fresh_extreme("NSE", "INFY", "high", QUOTE_HIGH, TODAY, bars))

    def test_missing_bars_fail_open(self):
        with patch("corporate_actions.sources.ohlc.get_ohlc", return_value=None):
            self.assertTrue(highlow.is_fresh_extreme("NSE", "INFY", "high", QUOTE_HIGH, TODAY))

    def test_bar_fetch_exception_fails_open(self):
        with patch("corporate_actions.sources.ohlc.get_ohlc", side_effect=RuntimeError("boom")):
            self.assertTrue(highlow.is_fresh_extreme("NSE", "INFY", "high", QUOTE_HIGH, TODAY))


class PendingAlertTests(unittest.TestCase):
    def setUp(self):
        self.quotes = {"INFY": QUOTE_HIGH, "RELIANCE": QUOTE_LOW}
        self.breakouts = {"INFY": "high", "RELIANCE": "low"}

    def test_each_chat_gets_its_universe_only(self):
        targets = [("1", {"enabled": True, "universe": "mylist"})]
        with patch.object(highlow, "highlow_symbols", side_effect=lambda chat, uni: {
            "1": ["INFY"],
            2: ["RELIANCE"],
        }[chat]):
            alerts = highlow.pending_alerts(targets, self.quotes, self.breakouts, set(), TODAY)
        self.assertEqual([(chat, symbol, kind) for chat, symbol, _, kind in alerts],
                         [("1", "INFY", "high")])

    def test_seen_key_blocks_the_same_alert_for_the_day(self):
        targets = [("1", {"enabled": True, "universe": "mylist"})]
        key = highlow.alert_key("1", TODAY, "INFY", "high")
        self.assertEqual(key, "hilo|1|2026-10-22|INFY|high")
        with patch.object(highlow, "highlow_symbols", return_value=["INFY"]):
            self.assertEqual(
                highlow.pending_alerts(targets, self.quotes, self.breakouts, {key}, TODAY), []
            )

    def test_nothing_to_send_without_a_breakout(self):
        targets = [("1", {"enabled": True, "universe": "mylist"})]
        with patch.object(highlow, "highlow_symbols", return_value=["INFY"]):
            self.assertEqual(
                highlow.pending_alerts(targets, self.quotes, {}, set(), TODAY), []
            )

    def test_defaults_are_on_over_nifty100(self):
        self.assertEqual(highlow.DEFAULT_HIGHLOW,
                         {"enabled": True, "universe": "nifty100"})


class FormatTests(unittest.TestCase):
    def test_high_alert_carries_the_stock_details(self):
        message = format_52w_alert("INFY", QUOTE_HIGH, "high")
        for expected in (
            "52-WEEK HIGH", "INFY", "Infosys Limited", "₹1,912.00",
            "Previous close", "52W Low", "52W range position", "From 52W low",
            "Day range", "P/E: <b>24.5x</b>", "momentum",
        ):
            self.assertIn(expected, message)

    def test_low_alert_explains_the_breakdown(self):
        message = format_52w_alert("RELIANCE", QUOTE_LOW, "low")
        self.assertIn("52-WEEK LOW", message)
        self.assertIn("₹1,139.00", message)
        self.assertIn("falling-knife", message)

    def test_company_name_is_html_escaped(self):
        quote = dict(QUOTE_HIGH, name="A & B <Ltd>")
        self.assertIn("A &amp; B &lt;Ltd&gt;", format_52w_alert("AB", quote, "high"))

    def test_sparse_quote_still_renders(self):
        message = format_52w_alert("TCS", {"price": 3000.0, "currency": "INR"}, "high")
        self.assertIn("52-WEEK HIGH", message)
        self.assertNotIn("n/a would make", message)

    def test_valuation_block_carries_pe_and_debt_to_equity(self):
        message = format_52w_alert("INFY", QUOTE_HIGH, "high", DETAILS)
        for expected in (
            "P/E: <b>42.3x</b>", "Sector P/E: 59.4x", "D/E: <b>0.27x</b>",
            "P/B: 21.8x", "Div yield: 1.96%", "Mkt cap: \u20b91,11,402 Cr",
            "ROE: 60.1%", "ROCE: 61.4%", "RSI 33.2", "Book value: \u20b9212.01",
            "EPS (TTM): \u20b9108.11", "Net margin: 13.4%",
            "Sector: Fast Moving Consumer Goods \u00b7 Packaged Foods",
        ):
            self.assertIn(expected, message)

    def test_quote_pe_is_used_when_details_are_missing(self):
        message = format_52w_alert("INFY", QUOTE_HIGH, "high", {})
        self.assertIn("P/E: <b>24.5x</b>", message)
        self.assertNotIn("D/E", message)

    def test_volume_is_shown_in_indian_units(self):
        quote = dict(QUOTE_HIGH, volume=14931109)
        self.assertIn("Volume: 1.49 Cr", format_52w_alert("INFY", quote, "high"))
        quote = dict(QUOTE_HIGH, volume=245000)
        self.assertIn("Volume: 2.5 L", format_52w_alert("INFY", quote, "high"))

    def test_cleared_level_wins_over_a_stale_52w_field(self):
        # Exchange 52w field still shows yesterday's 1908 while the print is
        # 1912 - the message must show the level the stock actually cleared.
        quote = dict(QUOTE_HIGH, wk52_high=1908.0)
        self.assertIn("₹1,912.00", format_52w_alert("INFY", quote, "high"))


class AlertDetailsTests(unittest.TestCase):
    def test_details_use_the_cached_screener_fundamentals(self):
        with patch("corporate_actions.sources.fundamentals.get_fundamentals",
                   return_value={"pe": 1.0}) as fetch:
            self.assertEqual(highlow.alert_details("INFY"), {"pe": 1.0})
        fetch.assert_called_once_with("INFY", with_screener=True)

    def test_details_failure_never_blocks_the_alert(self):
        with patch("corporate_actions.sources.fundamentals.get_fundamentals",
                   side_effect=RuntimeError("rate limited")):
            self.assertEqual(highlow.alert_details("INFY"), {})


class WatcherScanTests(unittest.TestCase):
    """run_watcher_once sends the 52-week alert through the real engine path."""

    def _poller(self):
        with patch("corporate_actions.poller.engine.storage.load_seen", return_value=set()):
            poller = Poller()
        poller._seen_stale = False
        return poller

    def _targets(self):
        return [("1", {"enabled": True, "universe": "mylist"})]

    def test_sends_one_alert_and_records_dedup(self):
        poller = self._poller()
        bars = _bars([(1, 1900.0, 1850.0)])
        with patch("corporate_actions.poller.engine.market_active", return_value=True), \
                patch("corporate_actions.poller.engine.storage.is_quiet", return_value=False), \
                patch("corporate_actions.poller.engine.watcher_module.watcher_targets",
                      return_value=[]), \
                patch("corporate_actions.poller.engine.highlow_module.highlow_targets",
                      return_value=self._targets()), \
                patch("corporate_actions.poller.engine.highlow_module.highlow_symbols",
                      return_value=["INFY"]), \
                patch("corporate_actions.poller.engine.watcher_module.fetch_quotes",
                      return_value={"INFY": QUOTE_HIGH}), \
                patch("corporate_actions.poller.engine.highlow_module.is_fresh_extreme",
                      return_value=True), \
                patch("corporate_actions.poller.engine.highlow_module.alert_details",
                      return_value=DETAILS) as mock_details, \
                patch("corporate_actions.poller.engine.send_message") as mock_send:
            sent = poller.run_watcher_once()
        self.assertEqual(sent, 1)
        mock_details.assert_called_once_with("INFY")
        self.assertIn("D/E: <b>0.27x</b>", mock_send.call_args[0][0])
        message = mock_send.call_args[0][0]
        self.assertIn("52-WEEK HIGH", message)
        self.assertIn("Infosys Limited", message)
        self.assertEqual(mock_send.call_args[1]["chat_id"], "1")
        self.assertIn(highlow.alert_key("1", date.today(), "INFY", "high"), poller._seen)

    def test_quiet_mode_suppresses_the_alert(self):
        poller = self._poller()
        with patch("corporate_actions.poller.engine.market_active", return_value=True), \
                patch("corporate_actions.poller.engine.storage.is_quiet", return_value=True), \
                patch("corporate_actions.poller.engine.watcher_module.watcher_targets",
                      return_value=[]), \
                patch("corporate_actions.poller.engine.highlow_module.highlow_targets",
                      return_value=self._targets()), \
                patch("corporate_actions.poller.engine.highlow_module.highlow_symbols",
                      return_value=["INFY"]), \
                patch("corporate_actions.poller.engine.watcher_module.fetch_quotes",
                      return_value={"INFY": QUOTE_HIGH}), \
                patch("corporate_actions.poller.engine.highlow_module.is_fresh_extreme",
                      return_value=True), \
                patch("corporate_actions.poller.engine.highlow_module.alert_details",
                      return_value=DETAILS), \
                patch("corporate_actions.poller.engine.send_message") as mock_send:
            sent = poller.run_watcher_once()
        self.assertEqual(sent, 0)
        mock_send.assert_not_called()

    def test_only_enabled_chats_are_scanned(self):
        poller = self._poller()
        with patch("corporate_actions.poller.engine.market_active", return_value=True), \
                patch("corporate_actions.poller.engine.watcher_module.watcher_targets",
                      return_value=[]), \
                patch("corporate_actions.poller.engine.highlow_module.highlow_targets",
                      return_value=[]), \
                patch("corporate_actions.poller.engine.watcher_module.fetch_quotes") as fetch:
            sent = poller.run_watcher_once()
        self.assertEqual(sent, 0)
        fetch.assert_not_called()

    def test_market_closed_scan_does_nothing(self):
        poller = self._poller()
        with patch("corporate_actions.poller.engine.market_active", return_value=False), \
                patch("corporate_actions.poller.engine.highlow_module.highlow_targets") as targets:
            self.assertEqual(poller.run_watcher_once(), 0)
        targets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
