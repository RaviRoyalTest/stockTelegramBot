"""52-week high/low must survive the trip into the stored JSON.

The 52-week extremes are read by the dashboard, the Telegram cards and the
stored screens alike, so a value that silently disappears on the way to disk
is invisible until someone reopens an old screen and sees a blank range.

Three invariants are pinned here:

  * a LIVE session row carries the trailing 52-week extremes (they arrive in
    the same Yahoo chart meta as the price, so they cost nothing extra);
  * a HISTORICAL session row does NOT carry them - chart meta always
    describes TODAY's trailing year, and stamping a past session with
    today's levels would be a lie the JSON could never undo;
  * compact_move_row keeps and rounds the fields when present, and omits
    the keys entirely when absent (never writes a null placeholder).
"""
import unittest
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from corporate_actions import snapshots as snapshots_mod
from corporate_actions.opening_report import data as d

IST = ZoneInfo("Asia/Kolkata")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _session_epochs():
    """Three daily bars ending on TODAY's India session.

    The live path only accepts a bar dated the market-local current date, so
    the fixture must be anchored on the wall clock instead of a frozen epoch
    that silently becomes a stale session as time passes.
    """
    today = datetime.now(IST).date()
    return [
        int(datetime.combine(today - timedelta(days=days), time(15, 30), IST).timestamp())
        for days in (2, 1, 0)
    ]


def _payload(meta_extra=None, timestamps=None):
    meta = {
        "longName": "Reliance Industries Limited",
        "symbol": "RELIANCE.NS",
        "regularMarketPrice": 1190.0,
    }
    meta.update(meta_extra or {})
    return {
        "chart": {
            "result": [{
                "meta": meta,
                "timestamp": list(timestamps or _session_epochs()),
                "indicators": {"quote": [{
                    "close": [1150.0, 1175.5, 1190.0],
                    "volume": [1000, 1200, 1500],
                }]},
            }]
        }
    }


class LiveSession52WeekTests(unittest.TestCase):
    def _fetch(self, start=None, end=None, meta_extra=None, timestamps=None):
        payload = _payload(meta_extra, timestamps)
        with patch.object(d, "_throttle_chart_req", lambda: None), \
                patch.object(d, "_quote_session",
                             lambda: type("S", (), {
                                 "get": lambda _self, url, timeout=None:
                                     _FakeResponse(payload),
                             })()):
            return d._fetch_regular_session("NSE", "RELIANCE", start, end)

    def test_live_row_carries_52_week_extremes(self):
        row = self._fetch(meta_extra={"fiftyTwoWeekHigh": 1611.8,
                                      "fiftyTwoWeekLow": 1152.2})
        self.assertIsNotNone(row)
        self.assertEqual(row["wk52_high"], 1611.8)
        self.assertEqual(row["wk52_low"], 1152.2)

    def test_missing_meta_52_week_stays_none_not_zero(self):
        row = self._fetch()
        self.assertIsNotNone(row)
        self.assertIsNone(row["wk52_high"])
        self.assertIsNone(row["wk52_low"])

    def test_stringy_meta_value_is_coerced(self):
        row = self._fetch(meta_extra={"fiftyTwoWeekHigh": "1611.8",
                                      "fiftyTwoWeekLow": "1152.2"})
        self.assertEqual(row["wk52_high"], 1611.8)
        self.assertEqual(row["wk52_low"], 1152.2)

    def test_historical_row_omits_52_week_fields(self):
        # The last bar is the completed session we ask for; that record must
        # not drag today's trailing extremes into a past session's record.
        stamps = _session_epochs()
        row = self._fetch(start=stamps[0], end=stamps[-1], timestamps=stamps,
                          meta_extra={"fiftyTwoWeekHigh": 1611.8,
                                      "fiftyTwoWeekLow": 1152.2})
        self.assertIsNotNone(row)
        self.assertNotIn("wk52_high", row)
        self.assertNotIn("wk52_low", row)


class CompactMoveRow52WeekTests(unittest.TestCase):
    def test_fields_carried_and_rounded(self):
        row = snapshots_mod.compact_move_row({
            "symbol": "RELIANCE", "price": 1190.0, "change_pct": 1.234,
            "wk52_high": 1611.804, "wk52_low": 1152.196,
        })
        self.assertEqual(row["wk52_high"], 1611.8)
        self.assertEqual(row["wk52_low"], 1152.2)

    def test_absent_fields_are_omitted_not_nulled(self):
        row = snapshots_mod.compact_move_row({
            "symbol": "RELIANCE", "price": 1190.0, "change_pct": 1.234,
        })
        self.assertNotIn("wk52_high", row)
        self.assertNotIn("wk52_low", row)

    def test_unusable_values_are_dropped(self):
        row = snapshots_mod.compact_move_row({
            "symbol": "RELIANCE", "wk52_high": "n/a", "wk52_low": None,
        })
        self.assertNotIn("wk52_high", row)
        self.assertNotIn("wk52_low", row)


if __name__ == "__main__":
    unittest.main()
