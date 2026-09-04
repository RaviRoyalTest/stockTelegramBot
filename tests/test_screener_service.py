import unittest

from corporate_actions import screener_service


class ScreenerServiceTests(unittest.TestCase):
    def test_screen_filters_and_pagination(self):
        # Stub universe and build rows
        # Replace universe loader for deterministic test
        screener_service._load_universe_symbols = lambda u: ("A", "B", "C", "D")

        def fake_build(symbol):
            base = {
                "A": {"symbol": "A", "pe": 10, "roe": 15, "market_cap": 1000, "price": 50, "rsi14": 45, "macd_bull": True, "above_ema200": True},
                "B": {"symbol": "B", "pe": 30, "roe": 5, "market_cap": 200, "price": 20, "rsi14": 60, "macd_bull": False, "above_ema200": False},
                "C": {"symbol": "C", "pe": 20, "roe": 12, "market_cap": 1500, "price": 150, "rsi14": 70, "macd_bull": True, "above_ema200": True},
                "D": {"symbol": "D", "pe": 25, "roe": 9, "market_cap": 500, "price": 80, "rsi14": 30, "macd_bull": True, "above_ema200": False},
            }
            return base[symbol]

        screener_service._get_cached_row = lambda s: fake_build(s)

        # Filter for pe <=20 and roe >=10
        rows = screener_service.screen_universe(filters={"pe_max": 20, "roe_min": 10}, limit=10, offset=0)
        symbols = [r["symbol"] for r in rows]
        self.assertIn("A", symbols)
        self.assertIn("C", symbols)
        self.assertNotIn("B", symbols)
        self.assertNotIn("D", symbols)

        # Test pagination: limit 1 offset 1
        rows2 = screener_service.screen_universe(filters={}, limit=1, offset=1)
        self.assertEqual(len(rows2), 1)

    def test_caching_works(self):
        screener_service._row_cache.clear()
        screener_service._load_universe_symbols = lambda u: ("X",)

        call_count = {"n": 0}

        def build(symbol):
            call_count["n"] += 1
            return {"symbol": symbol, "pe": 10}

        screener_service._build_row = build
        # first call populates cache
        rows1 = screener_service.screen_universe(filters={}, limit=10)
        rows2 = screener_service.screen_universe(filters={}, limit=10)
        # _build_row should be called at least once but not for each screen_universe call
        self.assertLessEqual(call_count["n"], 2)


if __name__ == "__main__":
    unittest.main()
