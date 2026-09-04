import unittest

from corporate_actions.dashboard_interface.tabs.screener import filter_candidates


class StockScreenerFilterTest(unittest.TestCase):
    def test_filters_by_fundamentals_and_technical_conditions(self):
        rows = [
            {
                "symbol": "RELIANCE",
                "pe": 24,
                "roe": 15,
                "debt_to_equity": 0.7,
                "market_cap": 1800000000000,
                "rsi14": 58,
                "macd_bull": True,
                "above_ema200": True,
            },
            {
                "symbol": "TCS",
                "pe": 31,
                "roe": 25,
                "debt_to_equity": 1.1,
                "market_cap": 1500000000000,
                "rsi14": 36,
                "macd_bull": False,
                "above_ema200": False,
            },
        ]

        filtered = filter_candidates(
            rows,
            {
                "pe_max": 25,
                "roe_min": 10,
                "debt_to_equity_max": 1.0,
                "market_cap_min": 1000000000000,
                "rsi_min": 40,
                "rsi_max": 70,
                "require_macd_bull": True,
                "require_above_ema200": True,
            },
        )

        self.assertEqual([row["symbol"] for row in filtered], ["RELIANCE"])

    def test_filters_handle_percent_roe_and_crore_market_cap(self):
        rows = [
            {
                "symbol": "ALPHA",
                "pe": 18,
                "roe": 15,
                "debt_to_equity": 0.7,
                "market_cap": 1_500_000_000,
                "rsi14": 62,
                "macd_bull": True,
                "above_ema200": True,
            },
            {
                "symbol": "BETA",
                "pe": 22,
                "roe": 7,
                "debt_to_equity": 0.4,
                "market_cap": 800_000_000,
                "rsi14": 58,
                "macd_bull": True,
                "above_ema200": True,
            },
        ]

        filtered = filter_candidates(
            rows,
            {
                "pe_max": 25,
                "roe_min": 10,
                "debt_to_equity_max": 1.0,
                "market_cap_min": 100,
                "rsi_min": 40,
                "rsi_max": 75,
                "require_macd_bull": True,
                "require_above_ema200": True,
            },
        )

        self.assertEqual([row["symbol"] for row in filtered], ["ALPHA"])


if __name__ == "__main__":
    unittest.main()
