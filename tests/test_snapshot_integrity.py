"""Snapshot market isolation, exact-session integrity and finalization lifecycle."""
import datetime as dt
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

from corporate_actions import config, snapshots
from corporate_actions.storage import snapshots as store


DAY = dt.date(2026, 9, 29)
IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")


class SnapshotStorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.object(config, "SNAPSHOT_FILE", self.root / "snapshots.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        capture = patch.object(store, "_capture_versioned")
        capture.start()
        self.addCleanup(capture.stop)

    def test_market_latest_survives_other_market_record(self):
        india = {"session": DAY.isoformat(), "market": "in", "finalized": True}
        us = {"session": DAY.isoformat(), "market": "us", "finalized": True}
        store.save_snapshots(india)
        store.save_snapshots(us)
        self.assertEqual(store.load_snapshots(), us)
        self.assertEqual(store.load_snapshots("in"), india)
        self.assertEqual(store.load_snapshots("US"), us)
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat(), "in"), india)

    def test_market_latest_prefers_newer_archive_over_global(self):
        latest = {"session": DAY.isoformat(), "market": "in"}
        store.archive_snapshot(latest)
        store.save_snapshots({"session": "2026-09-28", "market": "in"})
        self.assertEqual(store.load_snapshots("in"), latest)

    def test_corrupt_global_and_newest_archive_fall_back_safely(self):
        good = {"session": "2026-09-28", "market": "us"}
        store.archive_snapshot(good)
        directory = self.root / "snapshots"
        (directory / "2026-09-29-us.json").write_text("{bad", encoding="utf-8")
        (directory / "2026-09-30-us.json").write_text("[]", encoding="utf-8")
        config.SNAPSHOT_FILE.write_text("null", encoding="utf-8")
        self.assertEqual(store.load_snapshots(), {})
        self.assertEqual(store.load_snapshots("us"), good)
        self.assertEqual(store.load_snapshots("in"), {})

    def test_archive_content_must_match_date_and_market(self):
        directory = self.root / "snapshots"
        directory.mkdir()
        (directory / "2026-09-29-in.json").write_text(
            '{"session":"2026-09-28","market":"in"}', encoding="utf-8")
        (directory / "2026-09-29-us.json").write_text(
            '{"session":"2026-09-29","market":"in"}', encoding="utf-8")
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat(), "in"), {})
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat(), "us"), {})

    def test_date_validation_rejects_traversal_and_invalid_calendar_dates(self):
        invalid = ("../escape", "../../escape", "..\\escape", "/tmp/escape",
                   "2026-02-30", "2026-13-01", "20260929", "2026-9-29",
                   "2026-09-29/../../escape", "", None)
        with patch.object(store, "read_json") as reader, \
                patch.object(store, "write_json") as writer:
            for session in invalid:
                with self.subTest(session=session):
                    self.assertEqual(store.load_snapshot_archive(session, "in"), {})
                    self.assertIsNone(store.archive_snapshot({"session": session, "market": "in"}))
            reader.assert_not_called()
            writer.assert_not_called()
        self.assertFalse((self.root / "snapshots").exists())

    def test_history_lists_only_real_dates_and_known_market_suffixes(self):
        directory = self.root / "snapshots"
        directory.mkdir()
        for name in ("2026-09-29-in", "2026-09-28-us", "2026-02-30-in",
                     "2026-13-01-us", "2026-09-27-unknown", "20260929-in"):
            (directory / (name + ".json")).write_text("{}", encoding="utf-8")
        self.assertEqual(store.list_snapshot_dates("in"), ["2026-09-29"])
        self.assertEqual(store.list_snapshot_dates("us"), ["2026-09-28"])
        self.assertEqual(store.list_snapshot_dates(), ["2026-09-28", "2026-09-29"])
        self.assertEqual(store.list_snapshot_dates("../in"), [])
        self.assertEqual(store.load_snapshots("invalid"), {})
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat(), "invalid"), {})

    def test_missing_files_and_legacy_india_market_default(self):
        self.assertEqual(store.load_snapshots("in"), {})
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat()), {})
        legacy = {"session": DAY.isoformat()}
        store.save_snapshots(legacy)
        self.assertEqual(store.load_snapshots("in"), legacy)
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat()), legacy)
        self.assertEqual(store.load_snapshots("us"), {})


class SnapshotLifecycleIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(config, "SNAPSHOT_FILE", Path(tmp) / "snapshots.json"))
        self.stack.enter_context(patch.object(store, "_capture_versioned"))
        snapshots._auto_state.clear()
        self.addCleanup(snapshots._auto_state.clear)
        self.clock = self.stack.enter_context(patch.object(
            snapshots.market_hours, "local_now", return_value=dt.datetime(2026, 9, 29, 12, tzinfo=IST)))
        self.stack.enter_context(patch.object(snapshots, "latest_session_date", return_value=DAY))
        self.stack.enter_context(patch.object(snapshots, "IN_BLOCKS", (("in100", "NIFTY 100", lambda: ["GOOD"]),)))
        self.stack.enter_context(patch.object(snapshots, "get_index_universe", return_value=["GOOD"]))
        self.stack.enter_context(patch.object(snapshots, "get_bse_corporate_actions", return_value=[]))
        self.stack.enter_context(patch("corporate_actions.sources.get_nse_corporate_actions", return_value=[]))
        self.moves = self.stack.enter_context(patch.object(snapshots, "fetch_universe_moves", return_value=[
            {"symbol": "GOOD", "price": 105, "prev_close": 100, "change_pct": 5, "date": DAY.isoformat()},
            {"symbol": "STALE", "price": 150, "change_pct": 50, "date": "2026-09-28"},
        ]))
        self.gaps = self.stack.enter_context(patch.object(snapshots, "get_gap_change_on_date", return_value={
            "price": 105, "open": 98, "prev_close": 100, "gap_pct": -2, "date": DAY.isoformat(),
        }))

    def test_manual_intraday_then_same_session_auto_finalization(self):
        manual = snapshots.record_session_snapshot(market="in")
        self.assertFalse(manual["finalized"])
        self.assertEqual(manual["status"], "provisional")
        self.assertEqual([r["symbol"] for r in manual["top_gainers"]], ["GOOD"])
        self.assertEqual(manual["top_gainers"][0]["date"], DAY.isoformat())
        self.moves.assert_called_once_with("NSE", ["GOOD"], target_date=DAY)
        self.gaps.assert_called_once_with("NSE", "GOOD", DAY)

        # Recording the other market must not hide India's provisional capture.
        store.save_snapshots({"market": "us", "session": DAY.isoformat(), "finalized": True})
        self.assertEqual(snapshots.maybe_record_session_snapshot("in"), manual)
        self.assertEqual(snapshots._auto_state["in"]["attempts"], 0)
        self.clock.return_value = dt.datetime(2026, 9, 29, 15, 49, tzinfo=IST)
        self.assertEqual(snapshots.maybe_record_session_snapshot("in"), manual)
        self.clock.return_value = dt.datetime(2026, 9, 29, 15, 50, tzinfo=IST)
        final = snapshots.maybe_record_session_snapshot("in")
        self.assertTrue(final["finalized"])
        self.assertEqual(final["status"], "finalized")
        self.assertEqual(self.moves.call_count, 2)
        self.assertEqual(store.load_snapshot_archive(DAY.isoformat(), "in"), final)
        self.assertEqual(snapshots.maybe_record_session_snapshot("in"), final)
        self.assertEqual(self.moves.call_count, 2)

    def test_nonforced_after_close_manual_also_finalizes_provisional(self):
        snapshots.record_session_snapshot(market="in")
        self.clock.return_value = dt.datetime(2026, 9, 29, 16, tzinfo=IST)
        self.assertTrue(snapshots.record_session_snapshot(market="in")["finalized"])
        self.assertEqual(self.moves.call_count, 2)

    def test_us_record_does_not_block_india_bootstrap(self):
        store.save_snapshots({"market": "us", "session": DAY.isoformat(), "finalized": True})
        self.clock.return_value = dt.datetime(2026, 9, 29, 16, tzinfo=IST)
        doc = snapshots.maybe_record_session_snapshot("in")
        self.assertEqual(doc["market"], "in")
        self.assertTrue(doc["finalized"])
        self.assertEqual(store.load_snapshots("us")["market"], "us")

    def test_us_same_session_finalization_keeps_india_archive(self):
        india = {"session": DAY.isoformat(), "market": "in", "finalized": True}
        store.save_snapshots(india)
        self.clock.return_value = dt.datetime(2026, 9, 29, 12, tzinfo=ET)
        with patch.object(snapshots, "get_us_universe", return_value=["GOOD"]), \
                patch.object(snapshots, "get_us_market_caps", return_value={"GOOD": 300e9}):
            provisional = snapshots.record_session_snapshot(market="us")
            self.assertFalse(provisional["finalized"])
            self.assertEqual(provisional["universes"][0]["gainers"][0]["symbol"], "GOOD")
            self.clock.return_value = dt.datetime(2026, 9, 29, 16, 20, tzinfo=ET)
            final = snapshots.maybe_record_session_snapshot("us")
        self.assertTrue(final["finalized"])
        self.assertEqual(store.load_snapshots("in"), india)
        self.assertEqual(store.load_snapshots("us"), final)

    def test_unknown_session_reuses_market_archive_without_fetch(self):
        old = {"session": "2026-09-28", "market": "in", "finalized": True}
        store.save_snapshots(old)
        store.save_snapshots({"session": DAY.isoformat(), "market": "us"})
        with patch.object(snapshots, "latest_session_date", return_value=None):
            self.assertEqual(snapshots.record_session_snapshot(force=True, market="in"), old)
            self.assertEqual(snapshots.maybe_record_session_snapshot("in"), old)
        self.moves.assert_not_called()
        self.gaps.assert_not_called()

    def test_no_price_data_never_marks_actions_only_capture_final(self):
        self.moves.return_value = []
        self.gaps.return_value = None
        self.clock.return_value = dt.datetime(2026, 9, 29, 16, tzinfo=IST)
        with patch.object(snapshots, "get_bse_corporate_actions", return_value=[{"symbol": "X"}]):
            doc = snapshots.record_session_snapshot(market="in")
        self.assertFalse(doc["finalized"])

    def test_close_gate_uses_us_local_day_and_allows_prior_session_catchup(self):
        self.clock.return_value = dt.datetime(2026, 9, 29, 16, 19, tzinfo=ET)
        self.assertFalse(snapshots._session_can_finalize("us", DAY))
        self.clock.return_value = dt.datetime(2026, 9, 29, 16, 20, tzinfo=ET)
        self.assertTrue(snapshots._session_can_finalize("us", DAY))
        self.clock.return_value = dt.datetime(2026, 9, 30, 8, tzinfo=ET)
        self.assertTrue(snapshots._session_can_finalize("us", DAY))

    def test_busy_record_returns_requested_market_without_burning_attempt(self):
        india = {"market": "in", "session": DAY.isoformat()}
        store.save_snapshots(india)
        store.save_snapshots({"market": "us", "session": DAY.isoformat()})
        with snapshots._recording:
            self.assertEqual(snapshots.record_session_snapshot(market="in"), india)
            self.assertEqual(snapshots.maybe_record_session_snapshot("in"), india)
        self.assertEqual(snapshots._auto_state["in"]["attempts"], 0)
        self.moves.assert_not_called()

    def test_failed_sources_keep_old_doc_and_cap_attempts(self):
        old = {"market": "in", "session": "2026-09-28", "finalized": True}
        store.save_snapshots(old)
        self.clock.return_value = dt.datetime(2026, 9, 29, 16, tzinfo=IST)
        self.moves.return_value = []
        self.gaps.return_value = None
        for _ in range(4):
            self.assertEqual(snapshots.maybe_record_session_snapshot("in"), old)
        self.assertEqual(self.moves.call_count, snapshots.MAX_AUTO_ATTEMPTS)


class ExactSessionFetchTests(unittest.TestCase):
    def test_legacy_mover_api_gets_exact_date_window(self):
        seen = {}

        def legacy(exchange, symbols, start=None, end=None):
            seen.update(start=start, end=end)
            return [{"symbol": "X", "change_pct": 2}]

        for exchange, zone in (("NSE", IST), ("US", ET)):
            with self.subTest(exchange=exchange), patch.object(snapshots, "fetch_universe_moves", legacy):
                rows = snapshots._fetch_session_moves(exchange, ["X"], DAY)
            self.assertEqual(dt.datetime.fromtimestamp(seen["end"], dt.timezone.utc).date(), DAY)
            self.assertEqual(dt.datetime.fromtimestamp(seen["end"], zone).date(), DAY)
            self.assertEqual(seen["end"] - seen["start"], 14 * 86400)
            self.assertEqual(rows[0]["date"], DAY.isoformat())

    def test_real_bounded_provider_rejects_missing_target_bar(self):
        from corporate_actions.opening_report import data

        timestamps = [dt.datetime(2026, 9, day, 14, tzinfo=dt.timezone.utc).timestamp()
                      for day in (28, 29, 30)]
        response = Mock()
        for exchange in ("NSE", "US"):
            for closes, expected_count in (([100, 105, 200], 1), ([100, None, 200], 0)):
                response.json.return_value = {"chart": {"result": [{
                    "timestamp": timestamps,
                    "meta": {},
                    "indicators": {"quote": [{"close": closes, "volume": [10, 12, 14]}]},
                }]}}
                with self.subTest(exchange=exchange, closes=closes), \
                        patch.object(data, "_throttle_chart_req"), \
                        patch.object(data, "_quote_session") as session:
                    session.return_value.get.return_value = response
                    rows = snapshots._fetch_session_moves(exchange, ["X"], DAY)
                self.assertEqual(len(rows), expected_count)
                if rows:
                    self.assertEqual(rows[0]["price"], 105)
                    self.assertEqual(rows[0]["date"], DAY.isoformat())

    def test_undated_unbounded_mover_api_is_not_used(self):
        def unbounded(exchange, symbols):
            self.fail("An unbounded fetch could relabel stale bars")

        with patch.object(snapshots, "fetch_universe_moves", unbounded):
            self.assertEqual(snapshots._fetch_session_moves("US", ["X"], DAY), [])

    def test_stale_and_undated_gaps_are_rejected(self):
        for row in ({"gap_pct": -3}, {"gap_pct": -3, "date": "2026-09-28"}):
            with patch.object(snapshots, "get_gap_change_on_date", return_value=row):
                self.assertEqual(snapshots._fetch_gap("NSE", "X", DAY), ("X", None))

    def test_legacy_lifecycle_requires_verifiable_after_close_capture(self):
        base = {"session": DAY.isoformat(), "market": "in"}
        self.assertFalse(snapshots.is_snapshot_finalized(base))
        for captured, expected in (("2026-09-29T12:00:00+05:30", False),
                                   ("2026-09-29T15:49:00+05:30", False),
                                   ("2026-09-29T15:50:00+05:30", True),
                                   ("invalid", False)):
            with self.subTest(captured=captured):
                self.assertEqual(snapshots.is_snapshot_finalized({**base, "recorded_at": captured}), expected)
        self.assertFalse(snapshots.should_auto_record("in", base, latest=None))
        self.assertTrue(snapshots.should_auto_record("in", {**base, "market": "us"}, latest=DAY))
        self.assertFalse(snapshots.should_auto_record("in", {**base, "finalized": True}, latest=DAY))
        self.assertTrue(snapshots.should_auto_record("in", {**base, "session": "invalid"}, latest=DAY))


class SnapshotPollerIntegrityTests(unittest.TestCase):
    def test_poller_refreshes_both_markets_even_if_one_fails(self):
        from corporate_actions.poller import engine

        with patch.object(engine.storage, "load_seen", return_value=set()), \
                patch.object(engine.Poller, "_newest_seen_age_days", return_value=0), \
                patch.object(engine.Poller, "_warn_if_stale_seen"):
            poller = engine.Poller()
        poller._seen_stale = False
        with patch.object(engine, "_snapshots_auto_enabled", True), \
                patch.object(poller, "_collect_targets", return_value=[("1", [])]), \
                patch.object(poller, "_fetch_for_watchlist", return_value=([], [], [])), \
                patch.object(engine.storage, "save_seen"), \
                patch.object(engine.storage, "load_settings", return_value={"quiet": True}), \
                patch.object(snapshots, "maybe_record_session_snapshot", side_effect=[RuntimeError("India down"), {}]) as record:
            poller.run_once()
        self.assertEqual(record.call_args_list, [call(market="in"), call(market="us")])


if __name__ == "__main__":
    unittest.main()
