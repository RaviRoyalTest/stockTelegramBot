import unittest

from corporate_actions.email.daily import build_daily_lines


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


if __name__ == "__main__":
    unittest.main()
