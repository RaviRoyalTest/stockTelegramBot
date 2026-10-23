"""Consolidated 52-week mail block (email/daily.py + email/tables.py).

The mail must show the 52-week picture for the RECORDED session in one
consolidated table - one row per symbol, not the same stock repeated as a
gap-down, a gainer and a universe member. Two states are pinned here:

  * a session that recorded its levels -> the table is that session's band;
  * a session recorded before 52-week capture -> the block falls back to
    live levels and SAYS SO, because mailing today's band labelled as the
    session's would be a quiet lie.

No network: the live fallback is stubbed.
"""
import unittest
from unittest.mock import patch

from corporate_actions.email import daily as dm
from corporate_actions.email.tables import wk52_table


def _row(symbol, price, high, low, name=None):
    return {"symbol": symbol, "name": name or symbol, "price": price,
            "wk52_high": high, "wk52_low": low}


class RowCollectionTests(unittest.TestCase):
    def test_dedupes_across_gaps_gainers_losers_and_universes(self):
        doc = {
            "session": "2026-10-23",
            "gap_downs": [_row("AAA", 90.0, 200.0, 80.0)],
            "top_gainers": [_row("AAA", 90.0, 200.0, 80.0),
                            _row("BBB", 150.0, 200.0, 100.0)],
            "top_losers": [_row("BBB", 150.0, 200.0, 100.0)],
            "universes": [{"gainers": [_row("AAA", 90.0, 200.0, 80.0)],
                           "losers": [_row("CCC", 100.0, 200.0, 50.0)]}],
        }
        rows = dm.wk52_rows_from_snapshot(doc)
        # One row per symbol, ordered by position in the band (highest first):
        # BBB 50%, CCC 33%, AAA 8%.
        self.assertEqual([r["symbol"] for r in rows], ["BBB", "CCC", "AAA"])

    def test_percentages_and_range_position(self):
        doc = {"top_gainers": [_row("AAA", 150.0, 200.0, 100.0)]}
        row = dm.wk52_rows_from_snapshot(doc)[0]
        self.assertAlmostEqual(row["from_high_pct"], -25.0)
        self.assertAlmostEqual(row["from_low_pct"], 50.0)
        self.assertAlmostEqual(row["in_range_pct"], 50.0)

    def test_rows_without_levels_are_dropped_not_invented(self):
        doc = {
            "top_gainers": [{"symbol": "NOLV", "price": 10.0},
                            _row("OK", 10.0, 20.0, 5.0)],
            "top_losers": [{"symbol": "BAD", "price": 10.0,
                            "wk52_high": 5.0, "wk52_low": 20.0}],
        }
        self.assertEqual([r["symbol"] for r in dm.wk52_rows_from_snapshot(doc)], ["OK"])

    def test_empty_doc_is_empty(self):
        self.assertEqual(dm.wk52_rows_from_snapshot({}), [])
        self.assertEqual(dm.wk52_rows_from_snapshot(None), [])


class SectionRenderingTests(unittest.TestCase):
    def _recorded(self):
        return {
            "session": "2026-10-23", "market": "in",
            "top_gainers": [_row("AAA", 190.0, 200.0, 100.0),
                            _row("BBB", 102.0, 200.0, 100.0)],
            "top_losers": [_row("CCC", 120.0, 200.0, 100.0)],
        }

    def test_recorded_session_labels_the_date_and_counts_near_extremes(self):
        lines = dm.build_wk52_lines(snapshot=self._recorded())
        blob = "".join(lines)
        self.assertIn("2026-10-23", blob)
        self.assertIn("Recorded with the 2026-10-23 session", blob)
        # AAA is 5% off its high, BBB 49%, CCC 40% -> none within 2%.
        self.assertIn("Within 2% of high", blob)
        self.assertIn("Symbols", blob)
        self.assertIn("AAA", blob)

    def test_near_high_and_near_low_counts(self):
        doc = {"session": "2026-10-23",
               "top_gainers": [_row("AAA", 199.0, 200.0, 100.0),
                               _row("BBB", 101.0, 200.0, 100.0)]}
        blob = "".join(dm.build_wk52_lines(snapshot=doc))
        self.assertIn(">1<", blob)  # exactly one within 2% of each extreme

    def test_old_session_without_levels_says_so_and_invents_nothing(self):
        doc = {"session": "2026-10-22",
               "top_gainers": [{"symbol": "OLD", "price": 10.0}]}
        lines = dm.build_wk52_lines(snapshot=doc, allow_live_fallback=False)
        blob = "".join(lines)
        self.assertIn("No 52-week levels for this session", blob)
        self.assertNotIn("OLD", blob)

    def test_live_fallback_is_labelled_live(self):
        doc = {"session": "2026-10-22",
               "top_gainers": [{"symbol": "OLD", "price": 10.0}]}
        with patch.object(dm, "wk52_rows_live",
                          return_value=[_row("OLD", 10.0, 20.0, 5.0)]):
            blob = "".join(dm.build_wk52_lines(snapshot=doc))
        self.assertIn("Live levels", blob)
        self.assertIn("OLD", blob)
        self.assertNotIn("Recorded with the", blob)

    def test_table_caps_rows_and_handles_missing_price(self):
        rows = [_row(f"S{i}", 10.0, 20.0, 5.0) for i in range(20)]
        html = wk52_table(rows, limit=12)
        self.assertEqual(html.count("<tr>"), 13)  # header + 12 rows
        bare = wk52_table([{"symbol": "X", "wk52_high": 20.0, "wk52_low": 5.0}])
        self.assertIn("X", bare)

    def test_empty_table_is_a_muted_line_not_a_broken_table(self):
        self.assertIn("No 52-week levels", wk52_table([]))


if __name__ == "__main__":
    unittest.main()
