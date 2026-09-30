"""Tests for the centralized JSON snapshot store (storage/realtime.py).

Covers the naming standard, collision prevention, atomic writes, the
metadata envelope, JSONL appending, retention cleanup and the failure
paths required by the persistence contract. All writes go to temporary
directories - never to the real data/ tree.
"""
import json
import logging
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from corporate_actions.core.dates import IST
from corporate_actions.storage import realtime


class FilenameGenerationTests(unittest.TestCase):
    def test_filename_contains_name_date_time_and_extension(self):
        moment = datetime(2026, 9, 30, 18, 42, 15, 382451, tzinfo=IST)
        name = realtime.generate_snapshot_filename("market_data", timestamp=moment)
        self.assertEqual(name, "market_data_2026-09-30_18-42-15-382451.json")

    def test_naive_timestamp_is_treated_as_ist(self):
        naive = datetime(2026, 9, 30, 18, 42, 15)
        name = realtime.generate_snapshot_filename("quotes", timestamp=naive)
        self.assertIn("quotes_2026-09-30_18-42-15", name)

    def test_aware_timestamp_is_converted_to_ist(self):
        from datetime import timezone

        utc_moment = datetime(2026, 9, 30, 13, 12, 15, tzinfo=timezone.utc)
        name = realtime.generate_snapshot_filename("quotes", timestamp=utc_moment)
        # 13:12 UTC == 18:42 IST
        self.assertIn("quotes_2026-09-30_18-42-15", name)

    def test_data_name_is_normalized_to_a_safe_slug(self):
        moment = datetime(2026, 9, 30, 10, 0, 0, tzinfo=IST)
        name = realtime.generate_snapshot_filename("Market Data/Feed!", timestamp=moment)
        self.assertEqual(name, "market-data-feed_2026-09-30_10-00-00-000000.json")

    def test_empty_data_name_is_rejected(self):
        with self.assertRaises(ValueError):
            realtime.generate_snapshot_filename("   ")


class SaveSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

    def test_save_creates_date_partitioned_directory_and_valid_json(self):
        moment = datetime(2026, 9, 30, 18, 42, 15, 123456, tzinfo=IST)
        path = realtime.save_json_snapshot(
            "market_data", {"nifty": 25000.5}, timestamp=moment, base_dir=self.base_dir
        )
        self.assertEqual(
            path.relative_to(self.base_dir).parts[:4],
            ("market_data", "2026", "09", "30"),
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["data"], {"nifty": 25000.5})
        self.assertEqual(payload["metadata"]["captured_at"], "2026-09-30T18:42:15.123456+05:30")
        self.assertEqual(payload["metadata"]["timezone"], "Asia/Kolkata")

    def test_metadata_source_is_recorded(self):
        path = realtime.save_json_snapshot(
            "stock_quotes", [1, 2, 3], source="audit", base_dir=self.base_dir
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["metadata"]["source"], "audit")
        self.assertEqual(payload["data"], [1, 2, 3])

    def test_rapid_saves_never_overwrite_each_other(self):
        moment = datetime(2026, 9, 30, 18, 42, 15, tzinfo=IST)
        paths = [
            realtime.save_json_snapshot(
                "realtime_prices", {"tick": i}, timestamp=moment, base_dir=self.base_dir
            )
            for i in range(5)
        ]
        self.assertEqual(len({p.name for p in paths}), 5)
        for index, path in enumerate(paths):
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["data"]["tick"], index)

    def test_unicode_and_empty_payloads_round_trip(self):
        path = realtime.save_json_snapshot(
            "unicode_test", {"name": "महेश & Co", "items": []}, base_dir=self.base_dir
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["data"]["name"], "महेश & Co")
        self.assertEqual(payload["data"]["items"], [])

    def test_non_serializable_data_raises_and_logs(self):
        with self.assertLogs(realtime.log, level="ERROR"):
            with self.assertRaises(TypeError):
                realtime.save_json_snapshot("bad", {"obj": object()}, base_dir=self.base_dir)
        # no .tmp debris left behind
        self.assertEqual(list(self.base_dir.rglob("*.tmp")), [])

    def test_unwritable_directory_raises_and_logs(self):
        blocker = self.base_dir / "file"  # a FILE where a directory is needed
        blocker.write_text("x", encoding="utf-8")
        with self.assertLogs(realtime.log, level="ERROR"):
            with self.assertRaises((OSError, NotADirectoryError)):
                realtime.save_json_snapshot("blocked", {"a": 1}, base_dir=blocker)

    def test_successful_save_is_logged_at_info(self):
        with self.assertLogs(realtime.log, level="INFO") as captured:
            realtime.save_json_snapshot("logged_test", {"a": 1}, base_dir=self.base_dir)
        self.assertTrue(any("JSON snapshot saved" in line for line in captured.output))
        self.assertFalse(any("data" in line and "'a': 1" in line for line in captured.output))


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

    def test_no_temp_file_remains_after_success(self):
        path = realtime.save_json_snapshot("atomic", {"ok": True}, base_dir=self.base_dir)
        self.assertTrue(path.exists())
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_existing_snapshot_is_never_silently_replaced(self):
        moment = datetime(2026, 9, 30, 9, 0, 0, tzinfo=IST)
        first = realtime.save_json_snapshot(
            "hist", {"version": 1}, timestamp=moment, base_dir=self.base_dir
        )
        second = realtime.save_json_snapshot(
            "hist", {"version": 2}, timestamp=moment, base_dir=self.base_dir
        )
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["data"]["version"], 1)
        self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["data"]["version"], 2)


class AppendSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

    def test_appends_one_json_line_per_record(self):
        moment = datetime(2026, 9, 30, 10, 0, 0, tzinfo=IST)
        realtime.append_snapshot("ticks", {"p": 1}, timestamp=moment, base_dir=self.base_dir)
        realtime.append_snapshot("ticks", {"p": 2}, timestamp=moment, base_dir=self.base_dir)
        path = self.base_dir / "ticks" / "ticks_2026-09-30.jsonl"
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["record"]["p"] for record in lines], [1, 2])
        self.assertEqual(lines[0]["captured_at"], "2026-09-30T10:00:00.000000+05:30")


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

    def _make(self, name: str, age_days: float) -> Path:
        path = self.base_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        stamp = (datetime.now() - timedelta(days=age_days)).timestamp()
        import os

        os.utime(path, (stamp, stamp))
        return path

    def test_old_snapshots_removed_new_kept(self):
        old = self._make("oldfeed_2026-09-01_10-00-00-000000.json", 40)
        new = self._make("newfeed_2026-09-30_10-00-00-000000.json", 1)
        removed, reclaimed = realtime.cleanup_expired_snapshots(
            base_dir=self.base_dir, retention_days=30
        )
        self.assertGreaterEqual(removed, 1)
        self.assertGreaterEqual(reclaimed, 2)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_retention_disabled_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            removed, _ = realtime.cleanup_expired_snapshots(base_dir=self.base_dir)
        self.assertEqual(removed, 0)

    def test_env_var_enables_retention(self):
        old = self._make("oldfeed_2026-09-01_10-00-00-000000.json", 40)
        with mock.patch.dict("os.environ", {"JSON_RETENTION_DAYS": "30"}):
            removed, _ = realtime.cleanup_expired_snapshots(base_dir=self.base_dir)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())

    def test_hand_placed_files_are_never_touched(self):
        hand_placed = self._make("notes.json", 400)
        realtime.cleanup_expired_snapshots(base_dir=self.base_dir, retention_days=30)
        self.assertTrue(hand_placed.exists())

    def test_invalid_env_disables_retention_with_warning(self):
        with mock.patch.dict("os.environ", {"JSON_RETENTION_DAYS": "soon"}):
            with self.assertLogs(realtime.log, level="WARNING"):
                removed, _ = realtime.cleanup_expired_snapshots(base_dir=self.base_dir)
        self.assertEqual(removed, 0)


class UniquenessHelperTests(unittest.TestCase):
    def test_unique_path_avoids_existing_files(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            moment = datetime(2026, 9, 30, 10, 0, 0, tzinfo=IST)
            first = realtime.unique_snapshot_path("feed", timestamp=moment, base_dir=base)
            first.parent.mkdir(parents=True, exist_ok=True)
            first.write_text("{}", encoding="utf-8")
            second = realtime.unique_snapshot_path("feed", timestamp=moment, base_dir=base)
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
