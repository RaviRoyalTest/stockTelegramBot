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
from unittest.mock import patch

from corporate_actions import snapshots as snapshots_mod
from corporate_actions.opening_report import data as d


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _payload(meta_extra=None):
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
                "timestamp": [1759900000, 1760000000, 1760100000],
                "indicators": {"quote": [{
                    "close": [1150.0, 1175.5, 1190.0],
                    "volume": [1000, 1200, 1500],
                }]},
            }]
        }
    }


class LiveSession52WeekTests(unittest.TestCase):
    def _fetch(self, start=None, end=None, meta_extra=None):
        with patch.object(d, "_throttle_chart_req", lambda: None), \
                patch.object(d, "_quote_session",
                             lambda: type("S", (), {
                                 "get": lambda _self, url, timeout=None:
                                     _FakeResponse(_payload(meta_extra)),
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
        # 1760100000 = the last bar; asking for that completed session must
        # not drag today's trailing extremes into a past session's record.
        row = self._fetch(start=1759900000, end=1760100000,
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
