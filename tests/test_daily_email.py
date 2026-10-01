import unittest
from unittest.mock import patch

from corporate_actions.email.daily import build_combined_lines, build_daily_lines
from corporate_actions.email.daily import build_eod_store_lines, build_full_session_lines
from corporate_actions.email.daily import build_open_lines, get_scope


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


if __name__ == "__main__":
    unittest.main()
