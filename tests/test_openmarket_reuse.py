"""Tests for the reuse-first recorded-details layer.

Pins the behavior requested for /openmarket + the web pages:
  * /openmarket serves the recorded openclose report instantly (no fetch)
    and only "/openmarket now" triggers the live scan;
  * the web /api/openreport reuse helper (recorded_covers) accepts today's
    live doc covering the asked markets and rejects stale/foreign ones;
  * snapshot records are archived per date+market so recorded sessions stay
    reusable, and the archive lists/loads them back.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from corporate_actions import config
from corporate_actions import snapshots as snapshots_mod
from corporate_actions.bot import opening_report_commands as orc
from corporate_actions.opening_report import report as openreport
from corporate_actions.storage import snapshots as snapshots_store

IST = ZoneInfo("Asia/Kolkata")


def _doc(report=None, **overrides):
    doc = {
        "recorded_at": "2026-09-29T10:00:00+00:00",
        "recorded_by": "test",
        "mode": "live",
        "markets": ["in", "us"],
        "target_date": None,
        "report": report if report is not None else {"sections": []},
    }
    doc.update(overrides)
    return doc


class RecordedCoversTests(unittest.TestCase):
    def test_today_live_doc_covers_the_ask(self):
        doc = _doc(recorded_at=dt.datetime.now(IST).isoformat())
        self.assertTrue(openreport.recorded_covers(doc, ("in", "us")))
        # A recorded India+US file also answers an India-only ask.
        self.assertTrue(openreport.recorded_covers(doc, ("in",)))

    def test_yesterday_doc_is_stale(self):
        yesterday = dt.datetime.now(IST) - dt.timedelta(days=1)
        doc = _doc(recorded_at=yesterday.isoformat())
        self.assertFalse(openreport.recorded_covers(doc, ("in",)))

    def test_historical_doc_never_covers_a_live_ask(self):
        doc = _doc(recorded_at=dt.datetime.now(IST).isoformat(), mode="historical")
        self.assertFalse(openreport.recorded_covers(doc, ("in",)))

    def test_partial_coverage_is_rejected(self):
        doc = _doc(recorded_at=dt.datetime.now(IST).isoformat(), markets=["in"])
        self.assertFalse(openreport.recorded_covers(doc, ("in", "us")))

    def test_empty_or_broken_docs_are_rejected(self):
        self.assertFalse(openreport.recorded_covers({}, ("in",)))
        self.assertFalse(openreport.recorded_covers(None, ("in",)))
        self.assertFalse(openreport.recorded_covers({"report": {}}, ("in",)))


def _sections(market="in"):
    return [{"market": market, "universes": [
        {"key": "in100", "title": "NIFTY 100", "verified": 7, "target": 20,
         "gainers": [], "losers": []},
    ], "volume_computed": 3}]


class OpenmarketCommandTests(unittest.TestCase):
    def _sent(self):
        return []

    def test_no_records_hint_points_to_now(self):
        sent = []
        with patch.object(orc, "reply", lambda chat, msg, **kw: sent.append(msg)), \
                patch("corporate_actions.storage.load_openclose", return_value={}):
            orc.handle_openmarket(1, ["/openmarket"])
        self.assertTrue(sent and "openmarket now" in sent[0])

    def test_serves_recorded_report_without_fetching(self):
        sent = []
        doc = _doc(report={"sections": _sections(), "total_verified": 99,
                           "total_target": 20, "volume_computed": 5})
        with patch.object(orc, "reply", lambda chat, msg, **kw: sent.append(msg)), \
                patch.object(orc, "reply_messages", lambda chat, msgs, **kw: sent.extend(msgs)), \
                patch("corporate_actions.storage.load_openclose", return_value=doc), \
                patch.object(orc, "split_messages", lambda lines: lines), \
                patch("corporate_actions.opening_report.report.render_telegram",
                      return_value=["line"]) as render:
            orc.handle_openmarket(1, ["/openmarket"])
        render.assert_called_once()  # replay only - no collect/network
        self.assertTrue(any("recorded" in str(m).lower() for m in sent))

    def test_now_delegates_to_the_full_scan(self):
        with patch.object(orc, "handle_opening_report") as handler:
            orc.handle_openmarket(1, ["/openmarket", "now"])
        handler.assert_called_once_with(1, ["/openreport"])

    def test_market_slice_filters_sections_and_recomputes_totals(self):
        sent = []
        doc = _doc(report={"sections": _sections("in") + _sections("us"),
                           "total_verified": 14, "total_target": 40,
                           "volume_computed": 6})
        with patch.object(orc, "reply", lambda chat, msg, **kw: sent.append(msg)), \
                patch.object(orc, "reply_messages", lambda chat, msgs, **kw: sent.extend(msgs)), \
                patch("corporate_actions.storage.load_openclose", return_value=doc), \
                patch.object(orc, "split_messages", lambda lines: lines), \
                patch("corporate_actions.opening_report.report.render_telegram",
                      return_value=["line"]) as render:
            orc.handle_openmarket(1, ["/openmarket", "us"])
        shown = render.call_args[0][0]
        self.assertEqual([s["market"] for s in shown["sections"]], ["us"])
        self.assertEqual(shown["total_verified"], 7)  # recomputed for the slice
        self.assertEqual(shown["total_target"], 20)


class RecentRecordTests(unittest.TestCase):
    def _fresh_doc(self, markets=("in",)):
        recorded_at = (dt.datetime.now(dt.timezone.utc)
                       - dt.timedelta(minutes=10)).isoformat()
        return _doc(recorded_at=recorded_at, markets=list(markets),
                    report={"sections": _sections("in"),
                            "total_verified": 7, "total_target": 20,
                            "volume_computed": 3})

    def test_minutes_fresh_doc_reused(self):
        self.assertIsNotNone(openreport.recent_record(self._fresh_doc(), ("in",)))

    def test_stale_doc_rebuilds(self):
        doc = self._fresh_doc()
        doc["recorded_at"] = "2020-01-01T00:00:00+00:00"
        self.assertIsNone(openreport.recent_record(doc, ("in",)))

    def test_partial_coverage_rebuilds(self):
        self.assertIsNone(openreport.recent_record(self._fresh_doc(("in",)), ("in", "us")))


class OpenreportReuseWindowTests(unittest.TestCase):
    def _fresh_doc(self):
        recorded_at = (dt.datetime.now(dt.timezone.utc)
                       - dt.timedelta(minutes=10)).isoformat()
        return _doc(recorded_at=recorded_at,
                    report={"sections": _sections("in"),
                            "total_verified": 7, "total_target": 20,
                            "volume_computed": 3})

    def test_fresh_scan_replays_without_refetch(self):
        sent = []
        with patch.object(orc, "reply", lambda chat, msg, **kw: sent.append(msg)), \
                patch.object(orc, "reply_messages", lambda chat, msgs, **kw: sent.extend(msgs)), \
                patch("corporate_actions.storage.load_openclose",
                      return_value=self._fresh_doc()), \
                patch.object(orc, "split_messages", lambda lines: lines), \
                patch("corporate_actions.opening_report.report.render_telegram",
                      return_value=["line"]), \
                patch("corporate_actions.opening_report.collect_and_render",
                      side_effect=AssertionError("must not refetch")):
            orc.handle_opening_report(1, ["/openreport", "in"])
        self.assertTrue(any("Reusing" in str(m) for m in sent))

    def test_now_bypasses_reuse(self):
        sent = []
        with patch.object(orc, "reply", lambda chat, msg, **kw: sent.append(msg)), \
                patch.object(orc, "reply_messages", lambda chat, msgs, **kw: sent.extend(msgs)), \
                patch("corporate_actions.storage.load_openclose",
                      return_value=self._fresh_doc()), \
                patch.object(orc, "split_messages", lambda lines: lines), \
                patch("corporate_actions.opening_report.report.render_telegram",
                      return_value=["L2"]), \
                patch("corporate_actions.opening_report.collect_and_render",
                      return_value=(["L1"], {"sections": []})) as collect, \
                patch("corporate_actions.storage.save_openclose"):
            orc.handle_opening_report(1, ["/openreport", "in", "now"])
        collect.assert_called_once()
        self.assertTrue(any("Building" in str(m) for m in sent))


class SnapshotArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._patch = patch.object(
            config, "SNAPSHOT_FILE", Path(tmp.name) / "snapshots.json")
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_save_snapshots_archives_by_date_and_market(self):
        snapshots_store.save_snapshots({"session": "2026-09-29", "market": "in",
                                        "gap_downs": [{"symbol": "X"}]})
        archive = Path(config.SNAPSHOT_FILE.parent) / "snapshots" / "2026-09-29-in.json"
        self.assertTrue(archive.exists())
        loaded = snapshots_store.load_snapshot_archive("2026-09-29")
        self.assertEqual(loaded["gap_downs"], [{"symbol": "X"}])

    def test_list_and_market_filter(self):
        snapshots_store.save_snapshots({"session": "2026-09-29", "market": "in"})
        snapshots_store.save_snapshots({"session": "2026-09-26", "market": "us"})
        self.assertEqual(snapshots_store.list_snapshot_dates(),
                         ["2026-09-26", "2026-09-29"])
        self.assertEqual(snapshots_store.list_snapshot_dates(market="us"),
                         ["2026-09-26"])
        self.assertEqual(snapshots_store.load_snapshot_archive("2026-09-29", "us"), {})
        self.assertEqual(
            snapshots_store.load_snapshot_archive("2026-09-26", "us")["market"], "us")

    def test_undated_doc_is_not_archived(self):
        snapshots_store.save_snapshots({"market": "in"})
        self.assertEqual(snapshots_store.list_snapshot_dates(), [])

    def test_latest_read_still_works(self):
        snapshots_store.save_snapshots({"session": "2026-09-29", "market": "in"})
        self.assertEqual(snapshots_store.load_snapshots()["session"], "2026-09-29")


if __name__ == "__main__":
    unittest.main()
