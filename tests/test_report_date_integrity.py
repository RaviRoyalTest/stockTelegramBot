"""Section-local report identity, cache validity and non-destructive saves."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from corporate_actions import config
from corporate_actions.bot import opening_report_commands as commands
from corporate_actions.opening_report import data, report
from corporate_actions.storage import openclose as store

UTC = dt.timezone.utc
IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")


def section(market="in", day="2026-10-23", captured="2026-10-23T10:05:00+00:00",
            mode="live", verified=2):
    return {"market": market, "session_date": day, "captured_at": captured,
            "mode": mode, "universes": [{"verified": verified, "target": 20,
            "gainers": [{"symbol": "TEST"}] if verified else [], "losers": []}],
            "volume_computed": verified}


def document(*sections, **overrides):
    result = {"recorded_at": "2026-10-24T00:10:00+00:00", "mode": "live",
              "markets": [s["market"] for s in sections], "recorded_by": "test",
              "report": {"sections": list(sections)}}
    result.update(overrides)
    return result


class IdentityTests(unittest.TestCase):
    def test_us_close_is_us_session_not_ist_write_date(self):
        doc = document(section("us", captured="2026-10-23T20:05:00+00:00"))
        self.assertEqual(store._doc_date(doc), "2026-10-23")
        self.assertTrue(store.section_metadata(doc["report"]["sections"][0])["finalized"])

    def test_legacy_snapshot_uses_market_local_clock(self):
        legacy = {"market": "us", "snapshot": {"date": "23-Oct-2026", "mode": "live",
                  "time_local": "16:05", "state": "CLOSED", "tz_label": "ET"},
                  "universes": [{"verified": 2}]}
        normalized = store.section_metadata(legacy)
        self.assertEqual(normalized["session_date"], "2026-10-23")
        self.assertEqual(dt.datetime.fromisoformat(normalized["captured_at"]).astimezone(UTC).hour, 20)
        self.assertTrue(normalized["finalized"])
        self.assertNotIn("session_date", legacy)

    def test_legacy_universe_labels_are_supported(self):
        legacy = {"market": "in", "universes": [{"date": "23-Oct-2026", "time_local": "10:00",
                  "verified": 2}]}
        normalized = store.section_metadata(legacy)
        self.assertEqual(normalized["session_date"], "2026-10-23")
        self.assertEqual(normalized["mode"], "live")
        self.assertIsNotNone(normalized["captured_at"])
        self.assertFalse(normalized["finalized"])

    def test_wrapper_does_not_invent_legacy_identity(self):
        legacy = {"market": "us", "universes": [{"verified": 2}]}
        normalized = store.section_metadata(legacy, document(legacy))
        self.assertIsNone(normalized["session_date"])
        self.assertIsNone(normalized["captured_at"])
        self.assertFalse(normalized["finalized"])

    def test_conflicting_labels_reject_identity(self):
        value = section()
        value["snapshot"] = {"date": "22-Oct-2026", "mode": "live"}
        self.assertIsNone(store.section_metadata(value)["session_date"])

    def test_conflict_rejection_survives_repeated_normalization(self):
        value = section()
        value["snapshot"] = {"date": "22-Oct-2026", "mode": "live"}
        normalized = store.section_metadata(value)
        self.assertEqual(store.section_metadata(normalized), normalized)
        self.assertTrue(normalized["identity_conflict"])

    def test_closed_is_not_finalized_before_open(self):
        value = section("us", captured="2026-10-23T12:00:00+00:00")
        value["snapshot"] = {"state": "CLOSED"}
        self.assertFalse(store.section_metadata(value)["finalized"])

    def test_historical_today_and_future_are_not_finalized(self):
        for day in ("2026-10-23", "2026-10-26"):
            with self.subTest(day=day):
                value = section("us", day, "2026-10-23T14:00:00+00:00", mode="historical")
                self.assertFalse(store.section_metadata(value)["finalized"])

    def test_historical_future_capture_cannot_finalize_before_market_close(self):
        for market, tz, close in (("in", IST, (15, 30)), ("us", NY, (16, 0))):
            with self.subTest(market=market):
                before = dt.datetime(2026, 10, 23, close[0] - 1, close[1], tzinfo=tz)
                after = before + dt.timedelta(hours=1, minutes=5)
                value = section(market, captured=after.isoformat(), mode="historical")
                value["finalized"] = True
                self.assertFalse(store.section_metadata(value, now=before)["finalized"])
                self.assertTrue(store.section_metadata(value, now=after)["finalized"])
                self.assertFalse(report.validate_report_sections(
                    document(value), (market,), "2026-10-23",
                    require_finalized=True, now=before))

    def test_preclose_historical_capture_never_ages_into_final_close(self):
        for market, tz, hour in (("in", IST, 15), ("us", NY, 16)):
            before = dt.datetime(2026, 10, 23, hour - 1, 0, tzinfo=tz)
            after = dt.datetime(2026, 10, 23, 18, 0, tzinfo=tz)
            value = section(market, captured=before.isoformat(), mode="historical")
            value["snapshot"] = {"date": "23-Oct-2026", "time_local": "Session close",
                                 "mode": "historical", "state": "CLOSED"}
            self.assertFalse(store.section_metadata(value, now=after)["finalized"])
            self.assertFalse(report.validate_report_sections(
                document(value), (market,), "2026-10-23", require_finalized=True, now=after))

    def test_asof_selection_preserves_afterclose_finality_with_its_clock(self):
        future = section("us", "2030-10-23", "2030-10-23T20:05:00+00:00", mode="historical")
        asof = dt.datetime(2030, 10, 24, tzinfo=UTC)
        selected = report.select_report_sections(
            document(future), ("us",), "2030-10-23", require_finalized=True, now=asof)
        self.assertTrue(selected["finalized"])
        self.assertTrue(selected["sections"][0]["finalized"])

    def test_historical_completed_session_is_finalized(self):
        value = section("us", captured="2026-10-24T12:00:00+00:00", mode="historical")
        self.assertTrue(store.section_metadata(value)["finalized"])


class SelectionTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 23, 20, 10, tzinfo=UTC)

    def test_requested_subset_has_reconciled_totals_and_metadata(self):
        doc = document(section(), section("us", captured="2026-10-23T20:05:00+00:00"))
        before = copy.deepcopy(doc)
        shown = report.select_report_sections(doc, ("us",), "2026-10-23",
                                               require_finalized=True, now=self.now)
        self.assertEqual(shown["markets"], ["us"])
        self.assertEqual(shown["total_verified"], 2)
        self.assertEqual(shown["total_target"], 20)
        self.assertTrue(shown["finalized"])
        self.assertEqual(doc, before)

    def test_missing_market_empty_and_wrong_date_fail(self):
        doc = document(section())
        for markets, day in [(("in", "us"), "2026-10-23"), (("in",), "2026-10-22")]:
            self.assertFalse(report.validate_report_sections(doc, markets, day, now=self.now))
        self.assertFalse(report.recorded_covers(document(), ("in",)))

    def test_wrapper_freshness_does_not_refresh_retained_market(self):
        doc = document(section(), section("us", captured="2026-10-23T20:05:00+00:00"))
        self.assertFalse(report.validate_report_sections(doc, ("in", "us"), "2026-10-23",
                                                        max_age_min=45, now=self.now))
        self.assertTrue(report.validate_report_sections(doc, ("us",), "2026-10-23",
                                                       max_age_min=45, now=self.now))

    def test_per_market_today_across_ist_midnight(self):
        now = dt.datetime(2026, 10, 23, 20, 10, tzinfo=UTC)
        doc = document(section("in", "2026-10-24", "2026-10-23T20:05:00+00:00"),
                       section("us", captured="2026-10-23T20:05:00+00:00"))
        shown = report.select_report_sections(doc, ("in", "us"), now=now)
        self.assertTrue(shown)
        self.assertIsNone(shown["session_date"])
        self.assertTrue(report.validate_report_sections(
            doc, ("in", "us"), {"in": "2026-10-24", "us": "2026-10-23"}, now=now))

    def test_dated_live_close_can_satisfy_completed_session(self):
        doc = document(section("us", captured="2026-10-23T20:05:00+00:00"))
        self.assertTrue(report.validate_report_sections(doc, ("us",), dt.date(2026, 10, 23),
                                                       require_finalized=True, now=self.now))
        self.assertFalse(report.validate_report_sections(doc, ("us",), "2026-10-23",
                                                        mode="historical", now=self.now))

    def test_opening_capture_cannot_satisfy_close(self):
        doc = document(section("us", captured="2026-10-23T14:00:00+00:00"))
        self.assertFalse(report.validate_report_sections(doc, ("us",), "2026-10-23",
                                                        require_finalized=True, now=self.now))
        def clock(market, now=None):
            return self.now.astimezone(IST if market == "in" else NY)
        with patch.object(report, "local_now", side_effect=clock):
            self.assertFalse(report.recorded_covers(doc, ("us",)))
            self.assertFalse(report._record_is_current(doc, ("us",)))

    def test_recent_record_checks_each_capture_not_wrapper(self):
        us = section("us", captured="2026-10-23T20:05:00+00:00")
        doc = document(section(), us)
        def clock(market, now=None):
            return self.now.astimezone(IST if market == "in" else NY)
        with patch.object(report, "datetime") as date_clock, \
                patch.object(report, "local_now", side_effect=clock):
            date_clock.now.return_value = self.now
            self.assertIs(report.recent_record(doc, ("us",)), doc)
            self.assertIsNone(report.recent_record(doc, ("in", "us")))

    def test_failed_naive_future_and_duplicate_sections_rejected(self):
        values = [section(verified=0), section(captured="2026-10-23T10:00:00"),
                  section(captured="2026-10-24T10:00:00+00:00")]
        for value in values:
            self.assertFalse(report.validate_report_sections(document(value), ("in",),
                                                            "2026-10-23", now=self.now))
        self.assertFalse(report.validate_report_sections(document(section(), section()), ("in",),
                                                        "2026-10-23", now=self.now))


class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for patcher in (patch.object(config, "OPENREPORT_DIR", root / "openclose"),
                        patch.object(config, "OPENREPORT_FILE", root / "openclose.json"),
                        patch.object(store, "_capture_versioned")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_invalid_load_dates_never_construct_paths_or_read(self):
        invalid = ("", " ", " 2026-10-23 ", "../outside", "..\\outside",
                   "/tmp/outside", "C:\\outside", "2026-10-23/../../outside",
                   "2026-02-30", "2026-2-03", "20261023", "2026-W43-5",
                   "2026-10-23.json", "2026-10-23\x00", 123, False)
        with patch.object(store, "_dated_path") as path, \
                patch.object(store, "read_json") as read, \
                patch.object(store, "_file_lock") as lock:
            for value in invalid:
                with self.subTest(value=value):
                    self.assertEqual(store.load_openclose(value), {})
            path.assert_not_called()
            read.assert_not_called()
            lock.assert_not_called()

    def test_dated_path_rejects_traversal_and_invalid_calendar_dates(self):
        for value in ("../outside", "2026-02-29", "2026-2-01", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store._dated_path(value)
        self.assertEqual(store._dated_path("2024-02-29").name, "2024-02-29.json")

    def test_listing_and_latest_ignore_noncanonical_filenames(self):
        directory = config.OPENREPORT_DIR
        directory.mkdir(parents=True)
        for name in ("zzz.json", "2026-2-03.json", "9999-99-99.json", "20261024.json",
                     "2026-W43-6.json", "2026-10-24.JSON"):
            (directory / name).write_text('{"poison": true}', encoding="utf-8")
        (directory / "2026-10-25.json").mkdir()
        for day in ("2024-02-29", "2026-10-23"):
            (directory / f"{day}.json").write_text(json.dumps({"day": day}), encoding="utf-8")
        self.assertEqual(store.list_openclose_dates(), ["2024-02-29", "2026-10-23"])
        self.assertEqual(store.load_openclose(), {"day": "2026-10-23"})
        self.assertEqual(store.load_openclose("2024-02-29"), {"day": "2024-02-29"})
        self.assertEqual(store.load_openclose("2026-10-22"), {})

    def test_mixed_incoming_doc_is_partitioned_by_sections(self):
        store.save_openclose(document(section("in", "2026-10-24", "2026-10-24T04:30:00+00:00"),
                             section("us", captured="2026-10-23T20:05:00+00:00")))
        self.assertEqual(store.list_openclose_dates(), ["2026-10-23", "2026-10-24"])
        self.assertEqual(store.load_openclose("2026-10-23")["markets"], ["us"])
        self.assertEqual(store.load_openclose("2026-10-24")["markets"], ["in"])

    def test_failed_and_empty_builds_preserve_good_tables_and_capture(self):
        good = section("us", captured="2026-10-23T20:05:00+00:00")
        store.save_openclose(document(good))
        before = store.load_openclose("2026-10-23")["report"]
        for bad in (section("us", captured="2026-10-23T20:10:00+00:00", verified=0),
                    {**good, "closed": True}, {**good, "unavailable": True}):
            store.save_openclose(document(bad))
            self.assertEqual(store.load_openclose("2026-10-23")["report"], before)
        store.save_openclose(document())
        self.assertEqual(store.load_openclose("2026-10-23")["report"], before)

    def test_retained_sections_keep_own_capture_and_modes_reconcile(self):
        india = section(mode="historical")
        us = section("us", captured="2026-10-23T20:05:00+00:00")
        store.save_openclose(document(india, mode="historical", target_date="2026-10-23"))
        store.save_openclose(document(us))
        saved = store.load_openclose("2026-10-23")
        self.assertEqual(saved["mode"], "mixed")
        self.assertEqual(saved["report"]["mode"], "mixed")
        self.assertIsNone(saved["target_date"])
        self.assertIsNone(saved["report"]["target_date"])
        self.assertEqual(saved["markets"], ["in", "us"])
        self.assertEqual(saved["report"]["total_verified"], 4)
        self.assertEqual(saved["report"]["sections"][0]["captured_at"], india["captured_at"])

    def test_final_section_is_not_downgraded_by_partial_or_older_build(self):
        final = section("us", captured="2026-10-23T20:05:00+00:00")
        partial = section("us", captured="2026-10-23T14:00:00+00:00")
        store.save_openclose(document(final))
        store.save_openclose(document(partial))
        saved = store.load_openclose("2026-10-23")["report"]["sections"][0]
        self.assertEqual(saved["captured_at"], final["captured_at"])
        self.assertTrue(saved["finalized"])

    def test_save_adapter_does_not_recapture_legacy_section(self):
        old = {"market": "us", "snapshot": {"date": "23-Oct-2026", "mode": "live",
               "time_local": "10:00"}, "universes": [{"verified": 2}]}
        with patch("corporate_actions.storage.save_openclose"):
            saved = report.save_openclose_doc({"sections": [old]}, ("us",))
        captured = dt.datetime.fromisoformat(saved["report"]["sections"][0]["captured_at"])
        self.assertEqual(captured.astimezone(NY).hour, 10)
        self.assertFalse(saved["report"]["sections"][0]["finalized"])

    def test_migration_preserves_known_original_and_never_overwrites(self):
        raw = json.dumps(document(section()))
        config.OPENREPORT_FILE.write_text(raw, encoding="utf-8")
        self.assertEqual(store.migrate_openclose_file(), "2026-10-23")
        self.assertEqual(config.OPENREPORT_FILE.read_text(encoding="utf-8"), raw)
        path = config.OPENREPORT_DIR / "2026-10-23.json"
        path.write_text('{"sentinel": true}', encoding="utf-8")
        self.assertIsNone(store.migrate_openclose_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"sentinel": True})

    def test_ambiguous_legacy_save_is_not_filed_by_wrapper_day(self):
        store.save_openclose(document({"market": "us", "universes": [{"verified": 1}]}))
        self.assertEqual(store.list_openclose_dates(), [])

    def test_legacy_migration_leaves_ambiguous_original_untouched(self):
        raw = json.dumps(document({"market": "us", "universes": [{"verified": 1}]}))
        config.OPENREPORT_FILE.write_text(raw, encoding="utf-8")
        self.assertIsNone(store.migrate_openclose_file())
        self.assertEqual(config.OPENREPORT_FILE.read_text(encoding="utf-8"), raw)
        self.assertEqual(store.list_openclose_dates(), [])


class HistoricalTimezoneTests(unittest.TestCase):
    def test_windows_end_on_market_date_not_host_or_utc_day(self):
        for day in (dt.date(2026, 1, 23), dt.date(2026, 7, 23)):
            for market in ("in", "us"):
                start, end = data.session_window(market, day)
                self.assertEqual(data.session_date(end, market), day)
                self.assertLess(start, end)
                if market == "us":
                    self.assertEqual(data._utc_date(end), day + dt.timedelta(days=1))

    def test_ny_target_matching_does_not_pick_next_utc_day(self):
        day = dt.date(2026, 10, 23)
        _start, end = data.session_window("us", day)
        timestamps = [dt.datetime(2026, 10, 22, 9, 30, tzinfo=NY).timestamp(),
                      dt.datetime(2026, 10, 23, 9, 30, tzinfo=NY).timestamp()]
        self.assertEqual(data._historical_snapshot({"close": [100, 110], "volume": [10, 20]},
                                                   timestamps, end, "us"), (110, 20, 100, 10))

    def test_collect_uses_separate_market_windows_and_stamps_closed_sections(self):
        day = dt.date(2026, 10, 23)
        with patch.object(data, "has_session_on", return_value=True), \
                patch.object(report, "_collect_india", return_value=section()) as india, \
                patch.object(report, "_collect_us", return_value=section("us")) as us:
            built = report.collect(("in", "us"), day)
        self.assertEqual(india.call_args.args, data.session_window("in", day, report.HISTORY_WINDOW_DAYS))
        self.assertEqual(us.call_args.args, data.session_window("us", day, report.HISTORY_WINDOW_DAYS))
        for value in built["sections"]:
            self.assertEqual(value["session_date"], day.isoformat())
            self.assertEqual(value["mode"], "historical")
            self.assertIsNotNone(value["captured_at"])
        with patch.object(data, "has_session_on", return_value=False):
            closed = report.collect(("us",), day)["sections"][0]
        self.assertEqual(closed["session_date"], day.isoformat())
        self.assertFalse(closed["finalized"])

    def test_live_collect_uses_market_today(self):
        instant = dt.datetime(2026, 10, 23, 20, 5, tzinfo=UTC)
        def clock(market, now=None):
            return instant.astimezone(IST if market == "in" else NY)
        with patch.object(report, "local_now", side_effect=clock), \
                patch.object(report, "_market_status", return_value=(False, "closed")):
            built = report.collect(("in", "us"))
        self.assertEqual([s["session_date"] for s in built["sections"]],
                         ["2026-10-24", "2026-10-23"])


class LiveRowSessionTests(unittest.TestCase):
    def _fetch(self, market, now, timestamps, closes, volumes=None, historical=False):
        response = Mock()
        response.json.return_value = {"chart": {"result": [{
            "timestamp": timestamps,
            "meta": {"longName": "Test", "fiftyTwoWeekHigh": "150", "fiftyTwoWeekLow": 50},
            "indicators": {"quote": [{"close": closes, "volume": volumes or [100] * len(closes)}]},
        }]}}
        session = Mock()
        session.get.return_value = response
        exchange = "US" if market == "us" else "NSE"
        with patch.object(data, "_quote_session", return_value=session), \
                patch.object(data, "_throttle_chart_req"), \
                patch.object(data, "local_now", return_value=now) as clock:
            if historical:
                start, end = data.session_window(market, now.date())
                row = data._fetch_regular_session(exchange, "TEST", start, end)
                clock.assert_not_called()
            else:
                row = data._fetch_regular_session(exchange, "TEST")
                clock.assert_called_once_with(market)
        return row

    def test_stale_live_bar_is_rejected_for_both_markets(self):
        for market, tz in (("in", IST), ("us", NY)):
            with self.subTest(market=market):
                now = dt.datetime(2026, 10, 23, 14, tzinfo=tz)
                stamps = [dt.datetime(2026, 10, day, 10, tzinfo=tz).timestamp() for day in (21, 22)]
                self.assertIsNone(self._fetch(market, now, stamps, [100, 110]))

    def test_live_bar_date_and_wk52_metadata_survive(self):
        for market, tz in (("in", IST), ("us", NY)):
            with self.subTest(market=market):
                now = dt.datetime(2026, 10, 23, 14, tzinfo=tz)
                stamps = [dt.datetime(2026, 10, day, 10, tzinfo=tz).timestamp() for day in (22, 23)]
                row = self._fetch(market, now, stamps, [100, 110], [100, 125])
                self.assertEqual(row["session_date"], "2026-10-23")
                self.assertEqual(row["price"], 110)
                self.assertEqual(row["change_pct"], 10)
                self.assertEqual(row["volume_change_pct"], 25)
                self.assertEqual(row["wk52_high"], 150)
                self.assertEqual(row["wk52_low"], 50)

    def test_null_or_unstamped_today_bar_cannot_reuse_yesterday(self):
        now = dt.datetime(2026, 10, 23, 14, tzinfo=NY)
        stamps = [dt.datetime(2026, 10, day, 10, tzinfo=NY).timestamp() for day in (21, 22, 23)]
        self.assertIsNone(self._fetch("us", now, stamps, [100, 110, None]))
        self.assertIsNone(self._fetch("us", now, stamps[:2], [100, 110, 120]))
        self.assertIsNone(self._fetch("us", now, [], [100, 110, 120]))

    def test_null_reference_bar_keeps_original_timestamp_alignment(self):
        now = dt.datetime(2026, 10, 23, 14, tzinfo=NY)
        stamps = [dt.datetime(2026, 10, day, 10, tzinfo=NY).timestamp() for day in (21, 22, 23)]
        row = self._fetch("us", now, stamps, [100, None, 110], [50, None, 100])
        self.assertEqual(row["prev_close"], 100)
        self.assertEqual(row["volume_change_pct"], 100)
        self.assertEqual(row["session_date"], "2026-10-23")

    def test_live_session_guard_uses_market_date_across_utc_and_ist_boundaries(self):
        # This instant is the 23rd in NY, but the 24th in UTC and IST.
        now = dt.datetime(2026, 10, 24, 1, tzinfo=UTC)
        us_stamps = [dt.datetime(2026, 10, day, 10, tzinfo=NY).timestamp() for day in (22, 23)]
        self.assertEqual(self._fetch("us", now.astimezone(NY), us_stamps, [100, 110])
                         ["session_date"], "2026-10-23")
        in_stamps = [dt.datetime(2026, 10, day, 10, tzinfo=IST).timestamp() for day in (22, 23)]
        self.assertIsNone(self._fetch("in", now.astimezone(IST), in_stamps, [100, 110]))
        # India daily bars may be stamped at local midnight, on the prior UTC date.
        india_now = dt.datetime(2026, 10, 23, 10, tzinfo=IST)
        midnight_stamps = [dt.datetime(2026, 10, day, tzinfo=IST).timestamp() for day in (22, 23)]
        self.assertEqual(self._fetch("in", india_now, midnight_stamps, [100, 110])
                         ["session_date"], "2026-10-23")

    def test_future_live_session_and_first_usable_bar_are_rejected(self):
        now = dt.datetime(2026, 10, 23, 14, tzinfo=NY)
        stamps = [dt.datetime(2026, 10, day, 10, tzinfo=NY).timestamp() for day in (24, 25)]
        self.assertIsNone(self._fetch("us", now, stamps, [100, 110]))
        today = [dt.datetime(2026, 10, 23, 10, tzinfo=NY).timestamp()]
        self.assertIsNone(self._fetch("us", now, today, [100]))

    def test_historical_row_has_session_date_without_live_wk52_fields(self):
        now = dt.datetime(2026, 10, 23, 14, tzinfo=NY)
        stamps = [dt.datetime(2026, 10, day, 10, tzinfo=NY).timestamp() for day in (22, 23)]
        row = self._fetch("us", now, stamps, [100, 110], historical=True)
        self.assertEqual(row["session_date"], "2026-10-23")
        self.assertNotIn("wk52_high", row)
        self.assertNotIn("wk52_low", row)


class CommandTests(unittest.TestCase):
    def test_force_word_survives_delegation_in_any_position(self):
        for args in (["now"], ["now", "us"], ["us", "fresh"]):
            with patch.object(commands, "handle_opening_report") as handler:
                commands.handle_openmarket(1, ["/openmarket", *args])
            handler.assert_called_once_with(1, ["/openreport", *args])

    def test_openmarket_now_actually_collects_despite_cache(self):
        with patch.object(commands, "reply"), patch.object(commands, "reply_messages"), \
                patch("corporate_actions.storage.load_openclose", return_value=document(section())), \
                patch.object(report, "recent_record", side_effect=AssertionError("must bypass cache")), \
                patch("corporate_actions.opening_report.collect_and_render", return_value=(["built"], {"sections": []})) as collect, \
                patch.object(report, "save_openclose_doc"):
            commands.handle_openmarket(1, ["/openmarket", "now", "us"])
        collect.assert_called_once_with(("us",), None)

    def test_dated_replay_rejects_misfiled_section(self):
        wrong = document(section("us", "2026-10-22"))
        with patch("corporate_actions.storage.load_openclose", return_value=wrong), \
                patch.object(commands, "reply") as reply, \
                patch.object(commands, "reply_messages") as send:
            commands.handle_openmarket(1, ["/openmarket", "us", "2026-10-23"])
        send.assert_not_called()
        self.assertIn("No recorded session", reply.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
