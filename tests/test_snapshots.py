import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corporate_actions import config
from corporate_actions import snapshots as snapshots_mod
from corporate_actions.storage import snapshots as snapshots_store


class SnapshotHelpersTests(unittest.TestCase):
    def test_compact_gap_row_rounds_and_defaults(self):
        row = snapshots_mod.compact_gap_row(
            "RELIANCE",
            {"name": "", "price": 1322.456, "prev_close": 1303,
             "open": None, "gap_pct": -1.5123, "move_from_open_pct": 0.5},
        )
        self.assertEqual(row["symbol"], "RELIANCE")
        self.assertEqual(row["name"], "RELIANCE")
        self.assertEqual(row["price"], 1322.46)
        self.assertIsNone(row["open"])
        self.assertEqual(row["gap_pct"], -1.51)

    def test_trim_actions_sorts_dated_first_and_caps(self):
        rows = [
            {"symbol": "B", "subject": "Dividend", "ex_date": "-"},
            {"symbol": "A", "subject": "Bonus", "ex_date": "2026-09-20"},
            {"symbol": "C", "subject": "Split", "ex_date": "2026-09-10"},
        ]
        trimmed = snapshots_mod.trim_actions(rows, limit=2)
        self.assertEqual([row["symbol"] for row in trimmed], ["C", "A"])
        self.assertEqual(trimmed[0]["action"], "Split")

    def test_build_snapshot_doc_shape(self):
        doc = snapshots_mod.build_snapshot_doc(
            "2026-09-25", "nifty500", [{"symbol": "X"}],
            [{"symbol": "Y"}], [{"symbol": "Z"}], [{"symbol": "W"}],
            recorded_by="test",
        )
        self.assertEqual(doc["session"], "2026-09-25")
        self.assertEqual(doc["universe"], "nifty500")
        self.assertEqual(doc["recorded_by"], "test")
        self.assertTrue(doc["recorded_at"])
        self.assertEqual(len(doc["gap_downs"]), 1)

    def test_storage_roundtrip_in_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "snapshots.json"
            with patch.object(config, "SNAPSHOT_FILE", target):
                snapshots_store.save_snapshots({"session": "2026-09-25"})
                self.assertEqual(
                    snapshots_store.load_snapshots(), {"session": "2026-09-25"}
                )


if __name__ == "__main__":
    unittest.main()
