"""Auto-refresh of the recorded-sessions file (reuse first, fetch when stale).

Pins the behavior requested for the Sessions screen:
  * the stored day is reused while it covers the market's latest session;
  * a newer ended session is fetched automatically (poll cycle / page open);
  * an in-session or just-closed market never triggers a partial record;
  * a failing source is retried at most MAX_AUTO_ATTEMPTS times per day;
  * concurrent triggers (web button, /snap, auto) never double-fetch;
  * the poller hook only runs when enabled (hermetic tests by default).
"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from corporate_actions import config
from corporate_actions import snapshots as snapshots_mod
from corporate_actions.poller import engine as engine_mod

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")


class ShouldAutoRecordTests(unittest.TestCase):
    def test_empty_store_is_stale(self):
        self.assertTrue(snapshots_mod.should_auto_record("in", None, latest=dt.date(2026, 9, 29)))
        self.assertTrue(snapshots_mod.should_auto_record("in", {}, latest=dt.date(2026, 9, 29)))

    def test_older_session_is_stale(self):
        doc = {"market": "in", "session": "2026-09-26"}
        self.assertTrue(snapshots_mod.should_auto_record("in", doc, latest=dt.date(2026, 9, 29)))

    def test_current_session_is_reused(self):
        doc = {"market": "in", "session": "2026-09-29"}
        self.assertFalse(snapshots_mod.should_auto_record("in", doc, latest=dt.date(2026, 9, 29)))

    def test_unknown_latest_never_triggers(self):
        doc = {"market": "in", "session": "2026-09-26"}
        self.assertFalse(snapshots_mod.should_auto_record("in", doc, latest=None))

    def test_other_market_doc_is_not_replaced(self):
        us_doc = {"market": "us", "session": "2026-09-26"}
        in_doc = {"market": "in", "session": "2026-09-26"}
        self.assertFalse(snapshots_mod.should_auto_record("in", us_doc, latest=dt.date(2026, 9, 29)))
        self.assertFalse(snapshots_mod.should_auto_record("us", in_doc, latest=dt.date(2026, 9, 29)))


class MinutesSinceCloseTests(unittest.TestCase):
    def test_before_close_is_negative(self):
        now = dt.datetime(2026, 9, 29, 10, 15, tzinfo=IST)
        self.assertEqual(snapshots_mod._minutes_since_close("in", now), -315)

    def test_after_close_counts_up(self):
        now = dt.datetime(2026, 9, 29, 16, 10, tzinfo=IST)
        self.assertEqual(snapshots_mod._minutes_since_close("in", now), 40)

    def test_us_market_uses_et_close(self):
        now = dt.datetime(2026, 9, 29, 17, 5, tzinfo=ET)
        self.assertEqual(snapshots_mod._minutes_since_close("us", now), 65)


class MaybeRecordTests(unittest.TestCase):
    def setUp(self):
        snapshots_mod._auto_state.clear()
        self.addCleanup(snapshots_mod._auto_state.clear)

    def _tmp_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return patch.object(config, "SNAPSHOT_FILE", Path(tmp.name) / "snapshots.json")

    def test_fetches_when_stale_and_counts_attempts(self):
        with self._tmp_store(), \
                patch.object(snapshots_mod, "_minutes_since_close", return_value=60), \
                patch.object(snapshots_mod, "should_auto_record", return_value=True), \
                patch.object(snapshots_mod, "record_session_snapshot",
                             return_value={"session": "new"}) as rec:
            snapshots_mod.maybe_record_session_snapshot("in")
            snapshots_mod.maybe_record_session_snapshot("in")
            self.assertEqual(rec.call_count, snapshots_mod.MAX_AUTO_ATTEMPTS)
            rec.assert_called_with(market="in", force=True, recorded_by="auto-refresh")
            # Budget exhausted - no more attempts today.
            snapshots_mod.maybe_record_session_snapshot("in")
            self.assertEqual(rec.call_count, snapshots_mod.MAX_AUTO_ATTEMPTS)

    def test_reuses_file_when_current(self):
        with self._tmp_store(), \
                patch.object(snapshots_mod, "should_auto_record", return_value=False), \
                patch.object(snapshots_mod, "record_session_snapshot") as rec:
            doc = snapshots_mod.maybe_record_session_snapshot("in")
            rec.assert_not_called()
            self.assertEqual(doc, {})  # empty store echoed back, nothing fetched

    def test_waits_for_close_delay_before_recording(self):
        with self._tmp_store(), \
                patch.object(snapshots_mod, "_minutes_since_close", return_value=-100), \
                patch.object(snapshots_mod, "should_auto_record", return_value=True), \
                patch.object(snapshots_mod, "record_session_snapshot") as rec:
            snapshots_mod.maybe_record_session_snapshot("in")
            rec.assert_not_called()
            # The delay check must NOT burn the day's attempt budget.
            self.assertEqual(snapshots_mod._auto_state["in"]["attempts"], 0)

    def test_concurrent_record_returns_existing_without_fetch(self):
        with self._tmp_store():
            with snapshots_mod._recording:  # simulate a record in progress
                with patch.object(snapshots_mod.storage, "load_snapshots",
                                  return_value={"session": "stored"}) as loader:
                    doc = snapshots_mod.record_session_snapshot(
                        force=True, recorded_by="web")
            loader.assert_called_once()
            self.assertEqual(doc, {"session": "stored"})

    def test_is_recording_reflects_the_lock(self):
        self.assertFalse(snapshots_mod.is_recording())
        with snapshots_mod._recording:
            self.assertTrue(snapshots_mod.is_recording())
        self.assertFalse(snapshots_mod.is_recording())


class PollerHookTests(unittest.TestCase):
    def _run_cycle(self):
        poller = engine_mod.Poller()
        poller._seen_stale = False  # bypass the flood guard for this test
        with patch.object(engine_mod.Poller, "_collect_targets", return_value=[("1", [])]), \
                patch.object(engine_mod.Poller, "_fetch_for_watchlist", return_value=([], [], [])), \
                patch("corporate_actions.poller.engine.storage.save_seen"), \
                patch("corporate_actions.snapshots.maybe_record_session_snapshot") as rec:
            poller.run_once()
        return rec

    def test_disabled_by_default(self):
        self.assertFalse(engine_mod._snapshots_auto_enabled)

    def test_env_can_disable(self):
        with patch.dict(os.environ, {"SNAPSHOTS_AUTO_REFRESH": "false"}):
            self.assertFalse(engine_mod._snapshots_auto_default())
        with patch.dict(os.environ, {"SNAPSHOTS_AUTO_REFRESH": "0"}):
            self.assertFalse(engine_mod._snapshots_auto_default())
        with patch.dict(os.environ, {"SNAPSHOTS_AUTO_REFRESH": "1"}):
            self.assertTrue(engine_mod._snapshots_auto_default())

    def test_poll_cycle_kicks_auto_refresh_when_enabled(self):
        engine_mod._snapshots_auto_enabled = True
        try:
            rec = self._run_cycle()
            rec.assert_called_once()
        finally:
            engine_mod._snapshots_auto_enabled = False

    def test_poll_cycle_skips_auto_refresh_when_disabled(self):
        engine_mod._snapshots_auto_enabled = False
        rec = self._run_cycle()
        rec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
