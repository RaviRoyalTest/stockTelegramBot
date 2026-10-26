import json
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
        self.assertEqual(config.US_CAPS_FILE.parent.name, "data")
        self.assertEqual(
            {path.name for path in github.STATE_FILES},
            {"watchlist.json", "subscriptions.json", "settings.json",
             "seen_actions.json", "schedule.json", "snapshots.json",
             "snapshots", "openclose", "us_caps.json"},
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
                                         "report": self._report("in", 1, day="2026-09-29")})
                oc_store.save_openclose({"recorded_at": "2026-09-28T10:00:00+00:00",
                                         "report": self._report("in", 2, day="2026-09-28")})
                self.assertTrue((root / "data" / "openclose" / "2026-09-29.json").exists())
                self.assertTrue((root / "data" / "openclose" / "2026-09-28.json").exists())
                latest = oc_store.load_openclose()
                self.assertEqual(latest["report"]["total_verified"], 1)
                self.assertEqual(oc_store.list_openclose_dates(),
                                 ["2026-09-28", "2026-09-29"])
                specific = oc_store.load_openclose("2026-09-28")
                self.assertEqual(specific["report"]["total_verified"], 2)
                self.assertEqual(oc_store.load_openclose("2000-01-01"), {})

    def test_legacy_single_file_seeds_dated_and_retains_original(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            original = json.dumps({
                "recorded_at": "2026-09-29T10:00:00+00:00",
                "report": self._report("in", 1, day="2026-09-29"),
            })
            legacy = root / "data" / "openclose.json"
            legacy.write_text(original, encoding="utf-8")
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                day = oc_store.migrate_openclose_file()
            self.assertEqual(day, "2026-09-29")
            self.assertTrue((root / "data" / "openclose" / "2026-09-29.json").exists())
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            seeded = root / "data" / "openclose" / "2026-09-29.json"
            self.assertEqual(json.loads(seeded.read_text(encoding="utf-8")),
                             json.loads(original))
            # Second run is a no-op.
            with dir_patch, file_patch:
                self.assertIsNone(oc_store.migrate_openclose_file())

    def test_doc_date_uses_section_identity_not_wrapper_target(self):
        from corporate_actions.storage import openclose as oc_store

        doc = {"target_date": "2026-10-18",
               "recorded_at": "2026-09-29T10:00:00+00:00"}
        self.assertIsNone(oc_store._doc_date(doc))
        doc["report"] = self._report("in", 1, day="2026-09-29")
        self.assertEqual(oc_store._doc_date(doc), "2026-09-29")

    def test_ambiguous_legacy_original_is_retained_without_filing(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            legacy = root / "data" / "openclose.json"
            original = json.dumps({
                "recorded_at": "2026-09-29T10:00:00+00:00",
                "target_date": "2026-09-29",
                "report": {"sections": [{"market": "in", "universes": [
                    {"verified": 1, "target": 20}]}]},
            })
            legacy.write_text(original, encoding="utf-8")
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                self.assertIsNone(oc_store.migrate_openclose_file())
                self.assertEqual(oc_store.list_openclose_dates(), [])
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)

    def _report(self, market, verified, *, day="2026-10-08",
                captured_at=None, mode="live", finalized=False):
        captured_at = captured_at or f"{day}T{'04:30' if market == 'in' else '14:00'}:00+00:00"
        return {
            "sections": [{
                "market": market, "session_date": day,
                "captured_at": captured_at, "mode": mode, "finalized": finalized,
                "snapshot": {"date": day, "time_local": "10:00", "state": "OPEN"},
                "universes": [{"key": "k", "title": "T", "verified": verified,
                               "target": 20, "gainers": [], "losers": []}],
                "volume_computed": 1,
            }],
            "total_verified": verified, "total_target": 20, "volume_computed": 1,
        }

    def test_saves_merge_per_market_instead_of_wiping(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T04:00:00+00:00", "recorded_by": "a",
                    "mode": "live", "markets": ["in"],
                    "report": self._report("in", 60)})
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T14:00:00+00:00", "recorded_by": "b",
                    "mode": "live", "markets": ["us"],
                    "report": self._report("us", 40)})
                doc = oc_store.load_openclose("2026-10-08")
        sections = doc["report"]["sections"]
        self.assertEqual([s["market"] for s in sections], ["in", "us"])
        self.assertEqual(
            (doc["report"]["total_verified"], doc["report"]["total_target"]),
            (100, 40))
        self.assertEqual(doc["markets"], ["in", "us"])

    def test_rescan_replaces_only_its_market(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T04:00:00+00:00", "recorded_by": "a",
                    "mode": "live", "markets": ["in", "us"],
                    "report": {"sections": (
                        self._report("in", 1)["sections"]
                        + self._report("us", 2)["sections"])}})
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T10:00:00+00:00", "recorded_by": "b",
                    "mode": "live", "markets": ["in"],
                    "report": self._report("in", 60,
                        captured_at="2026-10-08T10:00:00+00:00", finalized=True)})
                doc = oc_store.load_openclose("2026-10-08")
        by_market = {s["market"]: s for s in doc["report"]["sections"]}
        self.assertEqual(by_market["in"]["universes"][0]["verified"], 60)
        self.assertEqual(by_market["us"]["universes"][0]["verified"], 2)
        self.assertEqual(by_market["us"]["captured_at"], "2026-10-08T14:00:00+00:00")
        self.assertFalse(by_market["us"]["finalized"])
        self.assertTrue(by_market["in"]["finalized"])
        self.assertFalse(doc["finalized"])

    def test_mixed_modes_are_explicit_and_keep_section_metadata(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T10:30:00+00:00",
                    "mode": "historical", "target_date": "2026-10-08",
                    "report": self._report("in", 60, mode="historical",
                        captured_at="2026-10-08T10:30:00+00:00", finalized=True)})
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T14:00:00+00:00", "mode": "live",
                    "report": self._report("us", 40)})
                doc = oc_store.load_openclose("2026-10-08")
        self.assertEqual(doc["mode"], "mixed")
        self.assertEqual(doc["report"]["mode"], "mixed")
        self.assertIsNone(doc["target_date"])
        self.assertIsNone(doc["report"]["target_date"])
        self.assertFalse(doc["finalized"])
        india, us = doc["report"]["sections"]
        self.assertEqual((india["mode"], india["finalized"]), ("historical", True))
        self.assertEqual((us["mode"], us["finalized"]), ("live", False))
        self.assertEqual(india["captured_at"], "2026-10-08T10:30:00+00:00")
        self.assertEqual(us["captured_at"], "2026-10-08T14:00:00+00:00")

    def test_empty_build_never_wipes_tables(self):
        from corporate_actions.storage import openclose as oc_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_patch, file_patch = self._patched_dir(root)
            with dir_patch, file_patch:
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T04:00:00+00:00", "recorded_by": "a",
                    "mode": "live", "markets": ["in"],
                    "report": self._report("in", 60)})
                oc_store.save_openclose({
                    "recorded_at": "2026-10-08T05:00:00+00:00", "recorded_by": "b",
                    "mode": "live", "markets": ["in"],
                    "report": {"sections": []}})
                doc = oc_store.load_openclose("2026-10-08")
        self.assertEqual(len(doc["report"]["sections"]), 1)


if __name__ == "__main__":
    unittest.main()
