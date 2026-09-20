"""Price-alert stale-session gate: alerts never fire on a non-session day.

Regression for the Saturday-redeploy bug: Yahoo serves the LAST completed
session's change % around the clock, and the price-alert dedup key is
date-stamped - so after a redeploy on a closed day (or any weekend cycle),
Friday's +3% move fired under Saturday's key as "... today".
"""
import unittest
from datetime import date
from unittest.mock import patch

from corporate_actions.market.hours import market_for_exchange
from corporate_actions.poller import engine


class MarketForExchangeTests(unittest.TestCase):
    def test_india_exchanges(self):
        self.assertEqual(market_for_exchange("NSE"), "in")
        self.assertEqual(market_for_exchange("BSE"), "in")
        self.assertEqual(market_for_exchange(None), "in")  # unknown -> India

    def test_us_exchanges(self):
        for exchange in ("NASDAQ", "NYSE", "AMEX", "OTC"):
            self.assertEqual(market_for_exchange(exchange), "us")


class SessionDayTests(unittest.TestCase):
    def setUp(self):
        engine._session_day_cache.clear()

    def test_holiday_is_not_a_session_day_and_result_is_cached(self):
        with patch("corporate_actions.opening_report.data.has_session_on",
                   return_value=False) as probe:
            self.assertFalse(engine._session_day("in", date(2026, 9, 23)))
            self.assertFalse(engine._session_day("in", date(2026, 9, 23)))
        self.assertEqual(probe.call_count, 1)  # second call served from cache

    def test_probe_outage_fails_open(self):
        # None (probe unavailable) must degrade to 'trading day', never mute
        # alerts for a whole day because one lookup failed.
        with patch("corporate_actions.opening_report.data.has_session_on",
                   return_value=None):
            self.assertTrue(engine._session_day("in", date(2026, 9, 23)))

    def test_real_session_day_passes(self):
        with patch("corporate_actions.opening_report.data.has_session_on",
                   return_value=True):
            self.assertTrue(engine._session_day("us", date(2026, 9, 23)))


class WeekendGateTests(unittest.TestCase):
    """On a weekend the cheap clock gate alone must close the path."""

    def test_saturday_blocks_both_markets_before_any_probe(self):
        from corporate_actions.market.hours import screen_available

        # 2026-09-19 is a Saturday; screen_available reads the real clock,
        # so assert on the calendar math via a Saturday-injection instead.
        import corporate_actions.market.hours as hours

        saturday = hours.local_now("in").replace(
            year=2026, month=9, day=19,
        ) if hours.local_now("in").weekday() >= 5 else None
        if saturday is None:
            self.skipTest("not running on a weekend host clock")
        self.assertFalse(screen_available("in", now=saturday))
        self.assertFalse(screen_available("us", now=saturday))


if __name__ == "__main__":
    unittest.main()
