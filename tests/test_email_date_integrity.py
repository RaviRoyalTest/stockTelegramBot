"""Mail must render section-local sessions, never the latest writer's date."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from corporate_actions import config, storage
from corporate_actions.email import daily
from corporate_actions.opening_report import report as reports
from corporate_actions.storage import settings


TODAY = "2026-10-23"
NOW = dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)


def section(market="in", day=TODAY, captured: str | None = "2026-10-23T04:30:00+00:00",
            mode="live", **extra):
    return {"market": market, "session_date": day, "captured_at": captured,
            "mode": mode, "universes": [{"title": market.upper(), "verified": 2,
            "target": 20, "gainers": [{"symbol": market.upper()}], "losers": []}],
            **extra}


def document(*sections):
    return {"recorded_at": NOW.isoformat(), "target_date": TODAY, "finalized": True,
            "markets": [s["market"] for s in sections],
            "report": {"target_date": TODAY, "sections": list(sections)}}


def close_section(**extra):
    return section(captured="2026-10-23T10:05:00+00:00", **extra)


class EmailDateIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(daily, "today_ist", return_value=dt.date(2026, 10, 23)))
        # Exercise the real selector with a deterministic, aware clock.
        self.stack.enter_context(patch.object(
            daily, "select_report_sections",
            side_effect=lambda *a, **kw: reports.select_report_sections(*a, now=NOW, **kw)))
        self.stack.enter_context(patch.object(config, "PROCESS_COMMANDS", True))
        self.stack.enter_context(patch.object(daily, "in_open_window", return_value=True))
        self.stack.enter_context(patch.object(daily, "in_close_window", return_value=True))
        self.stack.enter_context(patch.object(daily, "_india_traded_today", return_value=True))
        self.market_open = self.stack.enter_context(patch(
            "corporate_actions.market.hours.is_market_open", return_value=False))
        self.stack.enter_context(patch.object(daily, "email_configured", return_value=True))
        self.stack.enter_context(patch.object(storage, "get_user_settings", return_value={
            "email": "test@example.com", "daily_email": True, "email_scope": "both"}))
        self.load = self.stack.enter_context(patch.object(storage, "load_openclose", return_value={}))
        self.snapshots = self.stack.enter_context(patch.object(storage, "load_snapshots", return_value={}))
        self.collect = self.stack.enter_context(patch.object(reports, "collect", return_value={}))
        self.save = self.stack.enter_context(patch.object(reports, "save_openclose_doc"))
        self.mark = self.stack.enter_context(patch.object(daily, "patch_user_settings"))
        self.send = self.stack.enter_context(patch.object(daily, "send_email", return_value=(True, "")))
        self.open_lines = self.stack.enter_context(patch.object(
            daily, "build_morning_digest_lines", return_value=["open tables"]))
        self.close_lines = self.stack.enter_context(patch.object(
            daily, "build_close_digest_lines", return_value=["close tables"]))
        self.stack.enter_context(patch.object(storage, "list_openclose_dates", return_value=[]))
        self.stack.enter_context(patch.object(daily, "build_eod_store_lines", return_value=[]))
        self.stack.enter_context(patch.object(daily, "fetch_watchlist_quotes", return_value=[]))
        self.stack.enter_context(patch.object(storage, "load_watchlist", return_value=[]))
        self.stack.enter_context(patch.object(daily, "_chat_watchlist", return_value=[]))

    def test_capture_today_does_not_make_historical_session_current(self):
        doc = document(section(day="2026-10-22", mode="historical"))
        self.assertFalse(daily._recorded_doc_is_current(doc, TODAY))

    def test_wrapper_cannot_supply_missing_identity(self):
        unknown = {"market": "in", "universes": [{"verified": 2}]}
        self.assertFalse(daily._recorded_doc_is_current(document(unknown), TODAY))
        self.load.return_value = document(unknown)
        self.assertEqual(daily._ensure_fresh_india(TODAY), ({}, TODAY, False))

    def test_wrapper_date_does_not_override_valid_india_identity(self):
        doc = document(section())
        doc["recorded_at"] = "2026-10-24T00:00:00+00:00"
        doc["report"]["target_date"] = "2026-10-22"
        self.assertTrue(daily._recorded_doc_is_current(doc, TODAY))

    def test_open_reuses_unfinalized_india_only_and_reconciles_totals(self):
        self.market_open.return_value = True
        doc = document(section(), section("us", day="2026-10-22"))
        before = copy.deepcopy(doc)
        self.load.return_value = doc
        self.assertTrue(daily.maybe_send_open_email("chat"))
        shown = self.open_lines.call_args.args[1]
        self.assertEqual(shown["markets"], ["in"])
        self.assertEqual(shown["total_verified"], 2)
        self.assertEqual(shown["total_target"], 20)
        self.assertFalse(shown["finalized"])
        self.assertEqual(self.open_lines.call_args.args[2], TODAY)
        self.collect.assert_not_called()
        self.assertEqual(doc, before)

    def test_mixed_report_valid_us_does_not_rescue_stale_india(self):
        doc = document(section(day="2026-10-22"), close_section(market="us"))
        self.assertFalse(daily.maybe_send_open_email("chat", report=doc, force=True))
        self.assertFalse(daily.maybe_send_eod_email("chat", report=doc, force=True))
        self.send.assert_not_called()
        self.mark.assert_not_called()

    def test_force_open_still_rejects_missing_wrong_or_conflicting_identity(self):
        values = [section(day="2026-10-22"), section(captured=None),
                  section(snapshot={"date": "22-Oct-2026"}),
                  section(captured="2026-10-23T04:30:00"),
                  section(captured="2026-10-24T04:30:00+00:00")]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(daily.maybe_send_open_email("chat", document(value), force=True))
        self.send.assert_not_called()

    def test_duplicate_or_us_only_sections_are_not_an_india_mail(self):
        for doc in (document(section(), section()), document(section("us"))):
            with self.subTest(doc=doc):
                self.assertFalse(daily.maybe_send_open_email("chat", doc, force=True))
                self.assertFalse(daily.maybe_send_eod_email("chat", doc, force=True))
        self.send.assert_not_called()

    def test_eod_rejects_opening_capture_even_when_marked_finalized(self):
        opening = document(section(finalized=True))
        self.load.return_value = opening
        self.collect.return_value = opening
        self.assertFalse(daily.maybe_send_eod_email("chat", opening, force=True))
        self.close_lines.assert_not_called()
        self.save.assert_not_called()
        self.send.assert_not_called()
        self.mark.assert_not_called()

    def test_closed_market_rebuilds_today_finalized_close(self):
        self.load.return_value = document(section())
        self.collect.return_value = {"sections": [close_section(mode="historical")]}
        self.assertTrue(daily.maybe_send_eod_email("chat"))
        self.collect.assert_called_once_with(("in",), target_date=dt.date(2026, 10, 23))
        shown = self.close_lines.call_args.args[1]
        self.assertEqual(shown["markets"], ["in"])
        self.assertTrue(shown["finalized"])
        self.assertEqual(self.close_lines.call_args.args[2], TODAY)
        self.assertEqual(self.save.call_args.args[2], dt.date(2026, 10, 23))
        self.mark.assert_called_once_with("chat", {"last_eod_email": TODAY, "last_daily_email": TODAY})

    def test_close_reuses_valid_india_without_us_leakage(self):
        self.load.return_value = document(close_section(), section("us", day="2026-10-22"))
        self.assertTrue(daily.maybe_send_eod_email("chat"))
        self.assertEqual(self.close_lines.call_args.args[1]["markets"], ["in"])
        self.collect.assert_not_called()

    def test_rebuild_retains_us_in_storage_but_returns_only_india(self):
        self.market_open.return_value = True
        us = section("us", day="2026-10-22", captured="2026-10-22T20:05:00+00:00")
        self.load.return_value = document(us)
        self.collect.return_value = {"sections": [section()]}
        shown, label, current = daily._ensure_fresh_india(TODAY)
        self.assertTrue(current)
        self.assertEqual(label, TODAY)
        self.assertEqual(shown["markets"], ["in"])
        saved = self.save.call_args.args[0]
        self.assertEqual(saved["markets"], ["in", "us"])
        self.assertEqual(saved["sections"][1]["captured_at"], us["captured_at"])
        self.collect.assert_called_once_with(("in",))

    def test_no_finalized_rebuild_while_open_before_close_or_on_holiday(self):
        for market_open, close_window, traded in ((True, True, True), (False, False, True),
                                                   (False, True, False)):
            with self.subTest(market_open=market_open, close_window=close_window, traded=traded):
                self.market_open.return_value = market_open
                with patch.object(daily, "in_close_window", return_value=close_window), \
                        patch.object(daily, "_india_traded_today", return_value=traded):
                    self.assertEqual(daily._ensure_fresh_india(TODAY, True), ({}, TODAY, False))
        self.collect.assert_not_called()

    def test_failed_rebuild_does_not_trust_legacy_helper_current_flag(self):
        with patch.object(daily, "_ensure_fresh_india", return_value=(document(section()), TODAY, True)):
            self.assertFalse(daily.maybe_send_eod_email("chat", force=True))
        self.send.assert_not_called()

    def test_load_failure_still_attempts_valid_live_build(self):
        self.market_open.return_value = True
        self.load.side_effect = OSError("unreadable record")
        self.collect.return_value = {"sections": [section()]}
        shown, _, current = daily._ensure_fresh_india(TODAY)
        self.assertTrue(current)
        self.assertEqual(shown["markets"], ["in"])

    def test_save_failure_does_not_invalidate_built_tables(self):
        self.collect.return_value = {"sections": [close_section(mode="historical")]}
        self.save.side_effect = OSError("read only")
        self.assertTrue(daily.maybe_send_eod_email("chat"))

    def test_bad_or_failed_collection_is_not_mailed(self):
        for result in ({}, None, {"sections": [section("us")]},
                       {"sections": [close_section(day="2026-10-22")]},
                       {"sections": [close_section(error="failed")]}):
            with self.subTest(result=result):
                self.collect.return_value = result
                self.assertFalse(daily.maybe_send_eod_email("chat", force=True))
        self.collect.side_effect = RuntimeError("offline")
        self.assertFalse(daily.maybe_send_eod_email("chat", force=True))
        self.send.assert_not_called()

    def test_snapshot_fallback_is_india_scoped_and_current(self):
        self.snapshots.return_value = {"market": "in", "session": TODAY, "finalized": True,
                                       "top_gainers": [{"symbol": "IN", "change_pct": 1}]}
        self.assertTrue(daily.maybe_send_eod_email("chat", force=True))
        self.snapshots.assert_called_once_with("in")
        self.close_lines.assert_not_called()
        self.assertIn("Daily market digest", "\n".join(self.send.call_args.args[2]))

    def test_provisional_snapshot_cannot_bypass_close_finality(self):
        for lifecycle in ({}, {"finalized": False}, {"status": "provisional"}):
            with self.subTest(lifecycle=lifecycle):
                self.snapshots.return_value = {"market": "in", "session": TODAY,
                    "recorded_at": "2026-10-23T04:30:00+00:00",
                    "top_gainers": [{"symbol": "MORNING"}], **lifecycle}
                self.assertFalse(daily.maybe_send_eod_email("chat", force=True))
        self.send.assert_not_called()
        self.mark.assert_not_called()

    def test_legacy_post_close_snapshot_is_supported(self):
        self.snapshots.return_value = {"market": "in", "session": TODAY,
            "recorded_at": "2026-10-23T10:30:00+00:00",
            "top_gainers": [{"symbol": "CLOSE", "change_pct": 1}]}
        self.assertTrue(daily.maybe_send_eod_email("chat", force=True))

    def test_wrong_market_stale_or_undated_snapshot_is_not_fallback(self):
        for market, day in (("us", TODAY), ("in", "2026-10-22"), ("in", None)):
            with self.subTest(market=market, day=day):
                self.snapshots.return_value = {"market": market, "session": day, "finalized": True,
                                               "top_gainers": [{"symbol": "OLD"}]}
                self.assertFalse(daily.maybe_send_eod_email("chat", force=True))
        self.send.assert_not_called()
        self.mark.assert_not_called()

    def test_send_failure_does_not_mark_sent(self):
        self.send.return_value = (False, "offline")
        self.assertFalse(daily.maybe_send_eod_email("chat", document(close_section()), force=True))
        self.mark.assert_not_called()

    def test_latest_us_recap_selects_valid_finalized_us_only(self):
        us = section("us", day="2026-10-22", captured="2026-10-22T20:05:00+00:00")
        docs = {TODAY: document(section()), "2026-10-22": document(section(), us)}
        self.load.side_effect = lambda day=None: docs.get(day or "", {})
        with patch.object(storage, "list_openclose_dates", return_value=list(docs)):
            shown, label = daily._latest_us_report()
        self.assertEqual(label, "2026-10-22")
        self.assertEqual(shown["markets"], ["us"])
        self.assertEqual(shown["total_target"], 20)

    def test_previous_close_requires_finalized_india_not_just_archive_date(self):
        past = "2026-10-22"
        india = section(day=past, captured="2026-10-22T10:05:00+00:00")
        self.load.return_value = document(india, section("us"))
        with patch.object(storage, "list_openclose_dates", return_value=[past]):
            shown, label = daily._latest_recorded_before(TODAY)
            self.assertEqual(label, past)
            self.assertEqual(shown["markets"], ["in"])
            self.load.return_value = document(section(day=past,
                captured="2026-10-22T04:30:00+00:00"))
            self.assertEqual(daily._latest_recorded_before(TODAY), ({}, TODAY))

    def test_us_recap_does_not_trust_wrapper_identity(self):
        unknown = {"market": "us", "universes": [{"verified": 2}]}
        self.load.return_value = document(unknown)
        with patch.object(storage, "list_openclose_dates", return_value=[TODAY]):
            self.assertEqual(daily._latest_us_report(), ({}, ""))

    def test_legacy_section_local_labels_remain_supported(self):
        legacy = {"market": "in", "snapshot": {"date": "23-Oct-2026", "time_local": "10:00"},
                  "universes": [{"verified": 2, "target": 20}]}
        self.assertTrue(daily._recorded_doc_is_current(document(legacy), TODAY))
        self.assertFalse(daily._select_india(document(legacy), TODAY, True))


class IndiaSnapshotScopeTests(unittest.TestCase):
    def test_watchlist_fallback_reads_india_snapshot(self):
        with patch.object(daily, "fetch_watchlist_actions", return_value=[]), \
                patch.object(daily, "_chat_watchlist", return_value=[]), \
                patch.object(daily, "_owner_chat", return_value="owner"), \
                patch.object(storage, "load_snapshots", return_value={}) as load:
            daily.build_watchlist_block("chat")
        load.assert_called_once_with("in")

    def test_week52_default_reads_india_snapshot(self):
        with patch.object(storage, "load_snapshots", return_value={}) as load:
            daily.build_wk52_lines(allow_live_fallback=False)
        load.assert_called_once_with("in")

    def test_gaps_fallback_reads_india_snapshot(self):
        with patch.object(daily, "fetch_morning_gaps", return_value=([], [])), \
                patch.object(storage, "load_snapshots", return_value={}) as load:
            daily.build_gaps_lines()
        load.assert_called_once_with("in")


class MailSettingsIntegrityTests(unittest.TestCase):
    def test_mark_sent_patches_fresh_settings_without_losing_concurrent_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            original = {"chat": {"email": "new@example.com", "quiet": True,
                                  "email_sections": ["screener"]}, "other": {"quiet": False}}
            path.write_text(json.dumps(original), encoding="utf-8")
            with patch.object(config, "SETTINGS_FILE", path), \
                    patch.object(storage, "get_user_settings", side_effect=AssertionError("stale read")), \
                    patch.object(storage, "save_user_settings", side_effect=AssertionError("replacement write")):
                daily._mark_sent("chat", "last_open_email", TODAY)
                saved = settings.load_settings()
            self.assertEqual(saved["other"], original["other"])
            self.assertEqual(saved["chat"], {**original["chat"], "last_open_email": TODAY,
                                           "last_daily_email": TODAY})


if __name__ == "__main__":
    unittest.main()
