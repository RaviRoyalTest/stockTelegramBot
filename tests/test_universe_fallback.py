import unittest
from unittest.mock import patch

from corporate_actions.sources import universe


class UniverseFallbackTests(unittest.TestCase):
    def test_nifty500_falls_back_to_static_symbols_when_nse_csv_fails(self):
        class BrokenSession:
            def get(self, *args, **kwargs):
                raise RuntimeError("NSE unavailable")

        with patch.object(universe, "_quote_session", return_value=BrokenSession()):
            symbols = universe.get_index_universe("nifty500")

        self.assertTrue(symbols)
        self.assertIn("RELIANCE", symbols)
        self.assertIn("TCS", symbols)


if __name__ == "__main__":
    unittest.main()
