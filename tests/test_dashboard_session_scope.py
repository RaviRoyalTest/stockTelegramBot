"""Session-scope regression tests for the dashboard read APIs.

A recorded file is only an answer when it belongs to the REQUESTED market and
to a COMPLETED session. Both reads used to ignore that:

  * ``/api/openreport?date=...`` served any stored report - an opening scan or
    the other market's record - as that date's completed session;
  * ``/api/snapshots?date=...`` read the archive without forwarding ``market``,
    so an India request could be answered with the US file for the same date
    (and vice versa), while the history list ignored the scope entirely.

The recorder is also told which market was actually asked for, instead of the
other market's document that happened to be read.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import dashboard
from corporate_actions import config
from corporate_actions.opening_report import report as openreport


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _section(market: str, day: str, *, mode: str, clock: str, captured: str) -> dict:
    """A section whose identity is carried by its OWN labels (not the wrapper)."""
    return {
        "market": market,
        "snapshot": {"date": day, "mode": mode, "time_local": clock},
        "captured_at": captured,
        "session_date": day,
        "mode": mode,
        "universes": [{
            "key": "nifty100", "title": "NIFTY 100", "verified": 1, "target": 1,
            "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
            "losers": [],
        }],
        "indices": [],
        "volume_computed": 1,
    }


def _final_india(day: str = "2026-10-22") -> dict:
    return _section("in", day, mode="historical", clock="Session close",
                    captured=f"{day}T16:05:00+05:30")


def _opening_india(day: str = "2026-10-22") -> dict:
    return _section("in", day, mode="live", clock="09:20",
                    captured=f"{day}T09:20:00+05:30")


def _final_us(day: str = "2026-10-22") -> dict:
    return _section("us", day, mode="historical", clock="Session close",
                    captured=f"{day}T17:05:00-04:00")


def _doc(*sections: dict) -> dict:
    return {
        "recorded_at": "2026-10-22T12:00:00+00:00",
        "recorded_by": "test",
        "report": {"sections": list(sections), "mode": "historical"},
    }


class OpenReportHistoricalScopeTests(unittest.TestCase):
    """``/api/openreport?date=`` must not substitute another market or an open."""

    def _run(self, doc, market, date="2026-10-22"):
        built = {"sections": [_final_india()], "mode": "historical",
                 "total_verified": 1, "total_target": 1, "volume_computed": 1}
        with patch.object(dashboard.storage, "load_openclose", return_value=doc), \
                patch.object(openreport, "collect", return_value=built) as collect, \
                patch.object(openreport, "save_openclose_doc", return_value=None):
            response = asyncio.run(dashboard.api_openreport(market=market, date=date,
                                                            refresh=False))
        return _body(response), collect

    def test_finalized_requested_market_is_reused(self):
        body, collect = self._run(_doc(_final_india()), "in")
        self.assertTrue(body["from_cache"])
        self.assertEqual([s["market"] for s in body["sections"]], ["in"])
        collect.assert_not_called()

    def test_other_markets_record_is_not_an_answer(self):
        body, collect = self._run(_doc(_final_us()), "in")
        self.assertFalse(body["from_cache"])
        collect.assert_called_once()

    def test_opening_capture_is_not_a_completed_session(self):
        body, collect = self._run(_doc(_opening_india()), "in")
        self.assertFalse(body["from_cache"])
        collect.assert_called_once()

    def test_partial_market_coverage_is_not_an_answer(self):
        # "all" needs both markets; an India-only file must not satisfy it.
        body, collect = self._run(_doc(_final_india()), "all")
        self.assertFalse(body["from_cache"])
        collect.assert_called_once()

    def test_wrong_session_date_is_not_an_answer(self):
        body, collect = self._run(_doc(_final_india("2026-10-21")), "in")
        self.assertFalse(body["from_cache"])
        collect.assert_called_once()

    def test_invalid_market_still_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(dashboard.api_openreport(market="eu", date="", refresh=False))
        self.assertEqual(caught.exception.status_code, 400)


class RecordedHistoryScopeTests(unittest.TestCase):
    """``/api/openreport/recorded?market=`` filters sections per market."""

    def _run(self, doc, market):
        with patch.object(dashboard.storage, "load_openclose", return_value=doc), \
                patch.object(dashboard.storage, "list_openclose_dates",
                             return_value=["2026-10-22"]):
            response = asyncio.run(
                dashboard.api_openreport_recorded(date="2026-10-22", market=market))
        return _body(response)

    def test_market_filter_excludes_the_other_market(self):
        doc = _doc(_final_india(), _final_us())
        self.assertEqual([s["market"] for s in self._run(doc, "in")["report"]["sections"]],
                         ["in"])
        self.assertEqual([s["market"] for s in self._run(doc, "us")["report"]["sections"]],
                         ["us"])
        self.assertEqual(sorted(s["market"] for s in self._run(doc, "all")["report"]["sections"]),
                         ["in", "us"])

    def test_missing_market_for_that_date_is_a_404(self):
        with self.assertRaises(HTTPException) as caught:
            self._run(_doc(_final_us()), "in")
        self.assertEqual(caught.exception.status_code, 404)

    def test_wrapper_metadata_is_recomputed_from_sections(self):
        # The stored wrapper claims both markets; the US section was removed,
        # so the payload must not keep advertising it as fresh coverage.
        doc = _doc(_final_india())
        doc["report"].update(markets=["in", "us"], mode="historical", finalized=True)
        body = self._run(doc, "in")
        self.assertEqual(body["report"]["markets"], ["in"])
        self.assertIs(body["report"]["finalized"], True)
        self.assertEqual(body["report"]["session_date"], "2026-10-22")

    def test_provisional_capture_is_reported_as_not_final(self):
        body = self._run(_doc(_opening_india()), "in")
        self.assertIs(body["report"]["finalized"], False)


class SnapshotMarketScopeTests(unittest.TestCase):
    """``/api/snapshots?market=`` scopes the read AND the history list."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        history = self.root / "snapshots"
        history.mkdir()
        self.india = {"session": "2026-10-22", "market": "in", "universe": "nifty500",
                      "status": "finalized", "finalized": True,
                      "recorded_at": "2026-10-22T12:00:00+00:00",
                      "gap_downs": [{"symbol": "AAA"}], "top_gainers": [],
                      "top_losers": [], "corporate_actions": []}
        self.us = {"session": "2026-10-22", "market": "us", "universe": "mega",
                   "status": "finalized", "finalized": True,
                   "recorded_at": "2026-10-22T21:30:00+00:00",
                   "gap_downs": [{"symbol": "BBB"}], "top_gainers": [],
                   "top_losers": [], "corporate_actions": []}
        (history / "2026-10-22-in.json").write_text(json.dumps(self.india), encoding="utf-8")
        (history / "2026-10-22-us.json").write_text(json.dumps(self.us), encoding="utf-8")
        # The shared "latest" file currently holds the US record.
        (self.root / "snapshots.json").write_text(json.dumps(self.us), encoding="utf-8")
        for target, value in (
            (patch.object(config, "SNAPSHOT_FILE", self.root / "snapshots.json"), None),
            (patch.object(dashboard.snapshots_service, "is_recording", return_value=True), None),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(setattr, dashboard, "_snap_state",
                        dict(dashboard._snap_state))
        dashboard._snap_state["recording"] = False

    def _get(self, date="", market=""):
        return _body(asyncio.run(dashboard.api_snapshots(date=date, market=market)))

    def test_history_read_forwards_the_requested_market(self):
        self.assertEqual(self._get("2026-10-22", "in")["gap_downs"][0]["symbol"], "AAA")
        self.assertEqual(self._get("2026-10-22", "us")["gap_downs"][0]["symbol"], "BBB")

    def test_history_list_is_scoped_to_the_market(self):
        self.assertEqual(self._get("2026-10-22", "in")["dates"], ["2026-10-22"])
        self.assertEqual(self._get("2026-10-22", "us")["dates"], ["2026-10-22"])

    def test_latest_read_does_not_leak_the_other_market(self):
        # snapshots.json holds US; an India ask must fall back to India's
        # archived session instead of returning the US document.
        body = self._get("", "in")
        self.assertEqual(body["market"], "in")
        self.assertEqual(body["gap_downs"][0]["symbol"], "AAA")
        self.assertEqual(self._get("", "us")["gap_downs"][0]["symbol"], "BBB")

    def test_unrecorded_market_for_a_date_is_a_404(self):
        with self.assertRaises(HTTPException) as caught:
            self._get("2026-10-20", "in")
        self.assertEqual(caught.exception.status_code, 404)

    def test_invalid_market_is_a_400(self):
        with self.assertRaises(HTTPException) as caught:
            self._get("2026-10-22", "eu")
        self.assertEqual(caught.exception.status_code, 400)

    def test_legacy_call_without_market_keeps_latest_behaviour(self):
        body = self._get()
        self.assertEqual(body["gap_downs"][0]["symbol"], "BBB")


if __name__ == "__main__":
    unittest.main()
