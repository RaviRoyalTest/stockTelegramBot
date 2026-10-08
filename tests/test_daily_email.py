import unittest
from datetime import date, timedelta
from unittest.mock import patch

from corporate_actions import storage as storage_mod
from corporate_actions.email import daily as daily_mod
from corporate_actions.email.daily import build_combined_lines, build_daily_lines
from corporate_actions.email.daily import build_eod_store_lines, build_full_session_lines
from corporate_actions.email.daily import build_open_lines, get_scope
from corporate_actions.email.daily import maybe_send_eod_email, maybe_send_open_email


class DailyEmailTests(unittest.TestCase):
    def test_build_daily_lines_sections(self):
        snapshot = {
            "session": "2026-09-25",
            "universe": "nifty500",
            "gap_downs": [{"symbol": "A", "gap_pct": -3.5, "price": 100.0}],
            "top_gainers": [{"symbol": "B", "change_pct": 5.0, "price": 50.0}],
            "top_losers": [],
            "corporate_actions": [
                {"symbol": "C", "action": "Dividend", "ex_date": "2026-09-30"}
            ],
        }
        text = "\n".join(build_daily_lines(snapshot))
        self.assertIn("2026-09-25", text)
        self.assertIn("gap-downs", text)
        self.assertIn("Session movers", text)
        self.assertIn("Corporate actions", text)
        self.assertIn("/dailyemail off", text)

    def test_build_daily_lines_empty_snapshot(self):
        lines = build_daily_lines({})
        self.assertTrue(any("Daily market digest" in line for line in lines))

    def test_open_lines_colorful_tables(self):
        report = {
            "total_verified": 20, "total_target": 20,
            "sections": [{
                "market": "in",
                "snapshot": {"date": "01-Oct-2026", "time_local": "09:15", "state": "OPEN"},
                "universes": [{
                    "title": "NIFTY 100", "verified": 20, "target": 20,
                    "gainers": [{"symbol": "RELIANCE", "name": "Reliance",
                                 "price": 2500.5, "change_pct": 2.5,
                                 "volume": 1000000, "volume_change_pct": 60.0}],
                    "losers": [{"symbol": "TCS", "name": "TCS",
                                "price": 3000.0, "change_pct": -1.2,
                                "volume": 500000, "volume_change_pct": -10.0}],
                }],
                "indices": [{"label": "NIFTY 50", "level": 25000, "change_pct": 0.5}],
            }],
        }
        text = "\n".join(build_open_lines(report, "01-Oct-2026"))
        self.assertIn("RELIANCE", text)
        self.assertIn("rs-table", text)
        self.assertIn("pos", text)
        self.assertIn("Opening session screener", text)

    def test_scope_mapping(self):
        self.assertEqual(get_scope({"daily_email": True}), "both")
        self.assertEqual(get_scope({"daily_email": True, "email_scope": "open"}), "open")
        self.assertEqual(get_scope({"daily_email": True, "email_scope": "close"}), "close")
        self.assertEqual(get_scope({"daily_email": False}), "off")

    def test_full_session_single_banner(self):
        report = {
            "total_verified": 10, "total_target": 20,
            "sections": [{
                "market": "in",
                "snapshot": {"date": "01-Oct-2026", "time_local": "15:30", "state": "CLOSED"},
                "universes": [{
                    "title": "NIFTY 100", "verified": 10, "target": 20,
                    "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
                    "losers": [],
                }],
                "indices": [],
            }],
        }
        text = "\n".join(build_full_session_lines(report, "01-Oct-2026"))
        self.assertIn("Open + Close session screener", text)
        self.assertIn("RELIANCE", text)
        self.assertEqual(text.count("Open + Close session screener"), 1)

    def test_combined_lines_session_plus_stores(self):
        report = {
            "total_verified": 10, "total_target": 20,
            "sections": [{
                "market": "in",
                "snapshot": {"date": "01-Oct-2026", "time_local": "15:30", "state": "CLOSED"},
                "universes": [{
                    "title": "NIFTY 100", "verified": 10, "target": 20,
                    "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
                    "losers": [],
                }],
                "indices": [],
            }],
        }
        with patch("corporate_actions.email.daily.storage") as store:
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = []
            store.load_schedule_for.return_value = []
            store.load_snapshots.return_value = {}
            store.list_snapshot_dates.return_value = []
            store.list_openclose_dates.return_value = []
            text = "\n".join(build_combined_lines("123", report, "01-Oct-2026", []))
        self.assertIn("Open + Close session screener", text)
        self.assertIn("stored details", text)
        self.assertIn("RELIANCE", text)

    def test_eod_store_tables(self):
        with patch("corporate_actions.email.daily.storage") as store:
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = [{"symbol": "RELIANCE", "company": "Reliance", "exchange": "NSE"}]
            store.load_schedule_for.return_value = [{"interval_min": 180, "commands": ["/scan500"]}]
            store.load_snapshots.return_value = {}
            store.list_snapshot_dates.return_value = ["2026-10-01"]
            store.list_openclose_dates.return_value = ["2026-10-01"]
            quotes = [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.5}]
            text = "\n".join(build_eod_store_lines("123", quotes))
        self.assertIn("RELIANCE", text)
        self.assertIn("rs-table", text)
        self.assertIn("Watchlist", text)


def _live_report(label="2099-01-05"):
    return {
        "sections": [{
            "market": "in",
            "snapshot": {"date": label, "time_local": "10:00", "state": "OPEN"},
            "universes": [{
                "title": "NIFTY 100", "verified": 10, "target": 20,
                "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
                "losers": [],
            }],
            "indices": [],
        }],
        "total_verified": 10, "total_target": 20,
    }


class MarketGatedMailTests(unittest.TestCase):
    """No automatic mail when the market is closed; the morning mail builds
    a live opening scan when the recorded file is stale."""

    def setUp(self):
        daily_mod._TRADED_DAY_CACHE.clear()
        self.settings = {"email": "a@b.com", "daily_email": True, "email_scope": "both"}
        patches = [
            patch.object(daily_mod, "in_open_window", return_value=True),
            patch.object(daily_mod, "in_close_window", return_value=True),
            patch.object(daily_mod, "email_configured", return_value=True),
            patch.object(storage_mod, "get_user_settings", return_value=dict(self.settings)),
            patch.object(storage_mod, "save_user_settings"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        sender = patch.object(daily_mod, "send_email", return_value=(True, ""))
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def test_open_skipped_when_market_closed(self):
        with patch("corporate_actions.market.hours.is_market_open", return_value=False):
            self.assertFalse(maybe_send_open_email("123"))
        self.send.assert_not_called()

    def test_open_builds_live_when_recorded_stale(self):
        stale = {"recorded_at": "2020-01-01T00:00:00+00:00",
                 "report": _live_report("old")}
        live = _live_report()
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose", return_value=stale), \
                patch("corporate_actions.opening_report.report.collect", return_value=live) as collect, \
                patch("corporate_actions.opening_report.report.save_openclose_doc") as save:
            self.assertTrue(maybe_send_open_email("123"))
        collect.assert_called_once()
        save.assert_called_once()
        subject = self.send.call_args[0][1]
        self.assertIn("opening", subject)

    def _us_only_doc(self, stamped):
        return {
            "recorded_at": stamped, "markets": ["us"],
            "report": {
                "sections": [{
                    "market": "us",
                    "snapshot": {"date": "x", "time_local": "09:41", "state": "OPEN"},
                    "universes": [{
                        "title": "MEGA CAP ($200B+)", "verified": 0, "target": 20,
                        "gainers": [], "losers": [],
                    }],
                    "indices": [],
                }],
                "total_verified": 0, "total_target": 20,
            },
        }

    def test_open_rebuilds_when_recorded_lacks_india(self):
        """A US-only doc recorded today must NOT pass as the opening mail -
        India is live-built and merged alongside instead."""
        from corporate_actions.core.dates import today_ist
        from corporate_actions.email import daily as dm

        stamped = f"{today_ist().isoformat()}T10:00:00+05:30"
        live = _live_report()
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose",
                             return_value=self._us_only_doc(stamped)), \
                patch("corporate_actions.opening_report.report.collect",
                      return_value=live) as collect, \
                patch("corporate_actions.opening_report.report.save_openclose_doc") as save:
            self.assertTrue(maybe_send_open_email("123"))
        collect.assert_called_once_with(("in",))
        saved_report = save.call_args[0][0]
        self.assertEqual([s["market"] for s in saved_report["sections"]], ["in", "us"])
        self.assertEqual(saved_report["total_target"], 40)
        self.assertIn("opening", self.send.call_args[0][1])

    def test_merge_reports_replaces_stale_india(self):
        from corporate_actions.email.daily import _merge_reports

        base = self._us_only_doc("2026-10-07T10:00:00+05:30")
        base["report"]["sections"].append({
            "market": "in", "universes": [{"key": "in100", "verified": 0, "target": 20}],
            "volume_computed": 0,
        })
        base["report"]["total_verified"] = 0
        base["report"]["total_target"] = 40
        merged = _merge_reports(base["report"], _live_report()["sections"][0])
        self.assertEqual([s["market"] for s in merged["sections"]], ["in", "us"])
        self.assertEqual((merged["total_verified"], merged["total_target"]), (10, 40))

    def test_open_reuses_current_recorded_without_rebuild(self):
        from corporate_actions.core.dates import today_ist

        # Fixed mid-morning IST stamp: immune to UTC/IST midnight edges.
        stamped = f"{today_ist().isoformat()}T10:00:00+05:30"
        current = {"recorded_at": stamped, "report": _live_report()}
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose", return_value=current), \
                patch("corporate_actions.opening_report.report.collect",
                      side_effect=AssertionError("must not refetch")):
            self.assertTrue(maybe_send_open_email("123"))
        self.send.assert_called_once()

    def test_eod_skipped_when_no_session_today(self):
        yesterday = date.today() - timedelta(days=1)
        with patch("corporate_actions.opening_report.data.latest_session_date",
                   return_value=yesterday):
            self.assertFalse(maybe_send_eod_email("123"))
        self.send.assert_not_called()

    def test_eod_sends_when_traded_today(self):
        from corporate_actions.core.dates import today_ist

        stamped = f"{today_ist().isoformat()}T16:00:00+05:30"
        current = {"recorded_at": stamped, "report": _live_report()}
        with patch("corporate_actions.opening_report.data.latest_session_date",
                   return_value=date.today()), \
                patch.object(storage_mod, "load_openclose", return_value=current), \
                patch.object(storage_mod, "load_watchlist", return_value=[]):
            self.assertTrue(maybe_send_eod_email("123"))
        self.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
