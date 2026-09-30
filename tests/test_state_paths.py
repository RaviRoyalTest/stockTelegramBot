import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corporate_actions import config
from corporate_actions import github
from corporate_actions.storage.migrate import migrate_legacy_state_files


class StatePathsTests(unittest.TestCase):
    def test_state_files_live_under_data_dir(self):
        self.assertEqual(config.WATCHLIST_FILE.parent.name, "data")
        self.assertEqual(config.SUBSCRIPTIONS_FILE.parent.name, "data")
        self.assertEqual(config.SETTINGS_FILE.parent.name, "data")
        self.assertEqual(config.SEEN_FILE.parent.name, "data")
        self.assertEqual(config.SCHEDULE_FILE.parent.name, "data")
        self.assertEqual(config.SNAPSHOT_FILE.parent.name, "data")
        self.assertEqual(config.OPENREPORT_FILE.parent.name, "data")
        self.assertEqual(
            {path.name for path in github.STATE_FILES},
            {"watchlist.json", "subscriptions.json", "settings.json",
             "seen_actions.json", "schedule.json", "snapshots.json",
             "openclose"},
        )
        for path in github.STATE_FILES:
            self.assertEqual(path.parent.name, "data")

    def test_legacy_root_files_migrate_into_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "watchlist.json"
            legacy.write_text('[{"symbol": "RELIANCE"}]', encoding="utf-8")
            with patch.object(config, "WATCHLIST_FILE", root / "data" / "watchlist.json"), \
                 patch.object(config, "SUBSCRIPTIONS_FILE", root / "data" / "subscriptions.json"), \
                 patch.object(config, "SETTINGS_FILE", root / "data" / "settings.json"), \
                 patch.object(config, "SEEN_FILE", root / "data" / "seen_actions.json"), \
                 patch.object(config, "SCHEDULE_FILE", root / "data" / "schedule.json"):
                moved = migrate_legacy_state_files(base_dir=root)
            self.assertEqual(moved, ["watchlist.json"])
            self.assertFalse(legacy.exists())
            self.assertTrue((root / "data" / "watchlist.json").exists())
    def test_migration_never_overwrites_data_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "settings.json").write_text('{"a": 1}', encoding="utf-8")
            (root / "settings.json").write_text('{"b": 2}', encoding="utf-8")
            with patch.object(config, "WATCHLIST_FILE", root / "data" / "watchlist.json"), \
                 patch.object(config, "SUBSCRIPTIONS_FILE", root / "data" / "subscriptions.json"), \
                 patch.object(config, "SETTINGS_FILE", root / "data" / "settings.json"), \
                 patch.object(config, "SEEN_FILE", root / "data" / "seen_actions.json"), \
                 patch.object(config, "SCHEDULE_FILE", root / "data" / "schedule.json"):
                moved = migrate_legacy_state_files(base_dir=root)
            self.assertEqual(moved, [])
            self.assertEqual(
                (root / "data" / "settings.json").read_text(encoding="utf-8"), '{"a": 1}'
            )


class OpencloseDatedStoreTests(unittest.TestCase):
    """data/openclose/YYYY-MM-DD.json: dated writes, latest reads, migration."""

    def _patched_dir(self, root):
        return patch.object(config, "OPENREPORT_DIR", root / "data" / "openclose"), \
            patch.object(config, "OPENREPORT_FILE", root / "data" / "openclose.json")

    def test_save_writes_dated_file_and_load_reads_latest(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                oc_store.save_openclose({"recorded_at": "2026-09-29T10:00:00+00:00",
                                         "report": {"total_verified": 1}})
                oc_store.save_openclose({"recorded_at": "2026-09-28T10:00:00+00:00",
                                         "report": {"total_verified": 2}})
                self.assertTrue((root / "data" / "openclose" / "2026-09-29.json").exists())
                self.assertTrue((root / "data" / "openclose" / "2026-09-28.json").exists())
                latest = oc_store.load_openclose()
                self.assertEqual(latest["report"]["total_verified"], 1)
                self.assertEqual(oc_store.list_openclose_dates(),
                                 ["2026-09-28", "2026-09-29"])
                specific = oc_store.load_openclose("2026-09-28")
                self.assertEqual(specific["report"]["total_verified"], 2)
                self.assertEqual(oc_store.load_openclose("2000-01-01"), {})

    def test_legacy_single_file_seeds_dated_and_is_removed(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "openclose.json").write_text(
                '{"recorded_at": "2026-09-29T10:00:00+00:00", "report": {}}',
                encoding="utf-8",
            )
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                day = oc_store.migrate_openclose_file()
            self.assertEqual(day, "2026-09-29")
            self.assertTrue((root / "data" / "openclose" / "2026-09-29.json").exists())
            self.assertFalse((root / "data" / "openclose.json").exists())
            # Second run is a no-op.
            with dir_patch, file_patch:
                self.assertIsNone(oc_store.migrate_openclose_file())

    def test_doc_date_prefers_explicit_target(self):
        from corporate_actions.storage import openclose as oc_store

        self.assertEqual(
            oc_store._doc_date({"target_date": "2026-09-18",
                                "recorded_at": "2026-09-29T10:00:00+00:00"}),
            "2026-09-18",
        )


if __name__ == "__main__":
    unittest.main()
