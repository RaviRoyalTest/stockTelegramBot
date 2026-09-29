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
        self.assertEqual(
            {path.name for path in github.STATE_FILES},
            {"watchlist.json", "subscriptions.json", "settings.json",
             "seen_actions.json", "schedule.json", "snapshots.json"},
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


if __name__ == "__main__":
    unittest.main()
