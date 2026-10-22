"""Unknown-symbol handling: every stock command must suggest close names.

Regression for `/fundamentalreport waarertle` printing a blank report: Yahoo's
chart endpoint answers for any made-up ticker, so the lookup came back as a
``{'company': '<THE SYMBOL>'}`` shell - which every command mistook for a real
stock. The shell guard plus the shared suggestion pick-list now cover the
analysis, checklist, forecast, indicator, US, gappers, news and watchlist
commands alike.
"""
import unittest
from unittest.mock import patch

from corporate_actions.bot import (
    checklist_commands,
    forecast_commands,
    fundamentals_commands,
    helpers,
    us_commands,
    watchlist_commands,
)

SHELL = {"company": "WAARERTLE", "name": "WAARERTLE", "above_ema200": True}
REAL = {"company": "Reliance Industries Ltd", "pe": 40.9, "mcap_cr": 1602921.3}


class HasStockDataTests(unittest.TestCase):
    def test_made_up_ticker_shell_is_not_a_stock(self):
        self.assertFalse(helpers.has_stock_data("WAARERTLE", {}, SHELL))
        self.assertFalse(helpers.has_stock_data("WAARERTLE", {"price": None}, SHELL))

    def test_live_price_or_ratios_prove_a_real_stock(self):
        self.assertTrue(helpers.has_stock_data("RELIANCE", {"price": 1184.5}, {}))
        self.assertTrue(helpers.has_stock_data("RELIANCE", {}, REAL))

    def test_company_name_that_differs_from_the_symbol_is_real(self):
        self.assertTrue(helpers.has_stock_data("DMART", {}, {"company": "Avenue Supermarts Limited"}))

    def test_empty_lookup_is_not_a_stock(self):
        self.assertFalse(helpers.has_stock_data("XYZ", {}, {}))


class SymbolCandidateTests(unittest.TestCase):
    def test_nse_search_results_carry_ticker_company_and_exchange(self):
        with patch.object(helpers, "search_stocks",
                          return_value=[{"symbol": "WAAREERTL", "company": "Waaree Renewable"}]), \
                patch.object(helpers, "search_us_tickers", return_value=[]):
            candidates = helpers.symbol_candidates("waarertle", market="in")
        self.assertEqual(candidates[0]["symbol"], "WAAREERTL")
        self.assertEqual(candidates[0]["company"], "Waaree Renewable")
        self.assertEqual(candidates[0]["exchange"], "NSE")

    def test_exact_match_is_listed_first(self):
        with patch.object(helpers, "search_stocks",
                          return_value=[{"symbol": "RPOWER", "company": "Reliance Power"},
                                        {"symbol": "RELIANCE", "company": "Reliance Industries"}]), \
                patch.object(helpers, "search_us_tickers", return_value=[]):
            candidates = helpers.symbol_candidates("RELIANCE", market="in")
        self.assertEqual(candidates[0]["symbol"], "RELIANCE")

    def test_fuzzy_symbol_fallback_adds_the_company_name(self):
        with patch.object(helpers, "search_stocks", return_value=[]), \
                patch.object(helpers, "close_symbols", return_value=["GENESYS"]), \
                patch.object(helpers, "_nse_company_names",
                             return_value={"GENESYS": "Genesys International"}):
            candidates = helpers.symbol_candidates("GENESY", market="in")
        self.assertEqual(candidates, [
            {"symbol": "GENESYS", "company": "Genesys International", "exchange": "NSE"}
        ])

    def test_indian_market_never_calls_the_us_search(self):
        with patch.object(helpers, "search_stocks", return_value=[]), \
                patch.object(helpers, "close_symbols", return_value=[]), \
                patch.object(helpers, "search_us_tickers") as us_search:
            helpers.symbol_candidates("WAARERTLE", market="in")
        us_search.assert_not_called()

    def test_us_market_uses_the_us_search(self):
        with patch.object(helpers, "search_us_tickers",
                          return_value=[{"symbol": "AAPL", "name": "Apple Inc.",
                                         "exchange": "NASDAQ"}]):
            candidates = helpers.symbol_candidates("APPL", market="us")
        self.assertEqual(candidates[0]["symbol"], "AAPL")
        self.assertEqual(candidates[0]["exchange"], "NASDAQ")

    def test_duplicates_across_markets_are_collapsed(self):
        with patch.object(helpers, "search_stocks",
                          return_value=[{"symbol": "INFY", "company": "Infosys"}]), \
                patch.object(helpers, "search_us_tickers",
                             return_value=[{"symbol": "INFY", "name": "Infosys", "exchange": "NSE"}]):
            candidates = helpers.symbol_candidates("INFY")
        self.assertEqual(len(candidates), 1)


class NotFoundMessageTests(unittest.TestCase):
    def test_candidates_are_rendered_as_a_pick_list(self):
        message = helpers.not_found_message(
            "WAARERTLE",
            [{"symbol": "WAAREERTL", "company": "Waaree Renewable", "exchange": "NSE"}],
            command="fundamentalreport",
        )
        self.assertIn("No stock matched", message)
        self.assertIn("Did you mean one of these?", message)
        self.assertIn("<code>WAAREERTL</code> — Waaree Renewable (NSE)", message)
        self.assertIn("Try: <code>/fundamentalreport WAAREERTL</code>", message)

    def test_trailing_args_are_kept_in_the_try_line(self):
        message = helpers.not_found_message(
            "RELIANC", [{"symbol": "RELIANCE", "company": "", "exchange": "NSE"}],
            command="indicator", args=" rsi",
        )
        self.assertIn("/indicator RELIANCE rsi", message)

    def test_known_ticker_with_a_dead_feed_asks_for_a_retry(self):
        message = helpers.not_found_message(
            "RELIANCE", [{"symbol": "RELIANCE", "company": "Reliance", "exchange": "NSE"}],
        )
        self.assertIn("valid ticker", message)
        self.assertNotIn("Did you mean", message)

    def test_nothing_close_still_returns_guidance(self):
        message = helpers.not_found_message("ZZZQQQXYZ", [], market="in")
        self.assertIn("couldn't find anything close on NSE/BSE", message)
        self.assertIn("search by company name", message)


class ReplySuggestionsTests(unittest.TestCase):
    def _reply(self, *args, **kwargs):
        sent = []
        with patch.object(helpers, "reply", lambda chat, text, **extra: sent.append(text)), \
                patch.object(helpers, "symbol_candidates",
                             return_value=[{"symbol": "WAAREERTL", "company": "Waaree",
                                            "exchange": "NSE"}]):
            helpers.reply_suggestions("1", "WAARERTLE", *args, **kwargs)
        return sent[0]

    def test_addstock_gets_an_addstock_try_line(self):
        self.assertIn("/addstock WAAREERTL NSE", self._reply())

    def test_other_commands_reuse_their_own_name(self):
        self.assertIn("/forecast WAAREERTL", self._reply("forecast"))
        self.assertIn("/indicator WAAREERTL", self._reply("/indicator"))


class CommandRegressionTests(unittest.TestCase):
    """Each stock command stops and suggests instead of rendering a blank card."""

    def setUp(self):
        self.sent = []

    def _recorder(self, _chat, text, **kwargs):
        self.sent.append(text)

    def test_fundamentalreport_suggests_instead_of_a_blank_report(self):
        with patch.object(fundamentals_commands, "_resolve_quote", return_value=({}, False)), \
                patch.object(fundamentals_commands, "_resolve_fund", return_value=dict(SHELL)), \
                patch.object(fundamentals_commands, "reply_messages", self._recorder), \
                patch.object(helpers, "reply", self._recorder), \
                patch.object(helpers, "search_stocks",
                             return_value=[{"symbol": "WAAREERTL", "company": "Waaree"}]):
            fundamentals_commands.handle_fund_analysis("1", ["/fundamentalreport", "WAARERTLE"])
        self.assertTrue(self.sent)
        self.assertIn("Did you mean one of these?", self.sent[0])
        self.assertNotIn("FUNDAMENTAL REPORT", self.sent[0])

    def test_fundamentalanalyze_suggests_too(self):
        with patch.object(fundamentals_commands, "_resolve_quote", return_value=({}, False)), \
                patch.object(fundamentals_commands, "_resolve_fund", return_value=dict(SHELL)), \
                patch.object(fundamentals_commands, "reply_messages", self._recorder), \
                patch.object(helpers, "reply", self._recorder), \
                patch.object(helpers, "search_stocks",
                             return_value=[{"symbol": "WAAREERTL", "company": "Waaree"}]):
            fundamentals_commands.handle_single_stock_analysis(
                "1", ["/fundamentalanalyze", "WAARERTLE"]
            )
        self.assertIn("/fundamentalanalyze WAAREERTL", self.sent[0])

    def test_checklist_suggests_for_a_shell_lookup(self):
        with patch.object(checklist_commands, "_fetch_stock", return_value=({}, dict(SHELL), "INR")), \
                patch.object(checklist_commands, "reply_messages", self._recorder), \
                patch.object(helpers, "reply", self._recorder), \
                patch.object(helpers, "search_stocks",
                             return_value=[{"symbol": "WAAREERTL", "company": "Waaree"}]):
            checklist_commands.handle_single_checklist("1", "WAARERTLE")
        self.assertIn("Did you mean", self.sent[0])

    def test_forecast_suggests_for_a_shell_lookup(self):
        with patch.object(forecast_commands, "get_best_quote", return_value={}), \
                patch.object(forecast_commands, "get_quote", return_value={}), \
                patch.object(forecast_commands, "get_fundamentals", return_value=dict(SHELL)), \
                patch.object(forecast_commands, "normalise_fundamentals",
                             side_effect=lambda symbol, fund, quote: fund), \
                patch.object(forecast_commands, "reply_messages", self._recorder), \
                patch.object(helpers, "reply", self._recorder), \
                patch.object(helpers, "search_stocks",
                             return_value=[{"symbol": "WAAREERTL", "company": "Waaree"}]):
            forecast_commands.handle_forecast("1", ["/forecast", "WAARERTLE"])
        self.assertIn("Did you mean", self.sent[0])

    def test_usstock_suggests_for_an_unknown_ticker(self):
        with patch.object(us_commands, "get_quote", return_value={}), \
                patch.object(us_commands, "get_us_fundamentals", return_value={}), \
                patch.object(us_commands, "search_us_tickers",
                             return_value=[{"symbol": "AAPL", "name": "Apple Inc.",
                                            "exchange": "NASDAQ"}]), \
                patch.object(us_commands, "reply", self._recorder):
            us_commands.handle_us_stock("1", ["/usstock", "APPL"])
        self.assertIn("AAPL", self.sent[0])
        self.assertIn("Apple Inc.", self.sent[0])

    def test_news_typo_suggests_close_names(self):
        with patch.object(watchlist_commands, "get_stock_news", return_value=[]), \
                patch.object(watchlist_commands, "known_symbol", return_value=False), \
                patch.object(watchlist_commands, "send_message"), \
                patch.object(helpers, "reply", self._recorder), \
                patch.object(helpers, "search_stocks",
                             return_value=[{"symbol": "WAAREERTL", "company": "Waaree"}]):
            watchlist_commands.handle_news("1", ["/news", "WAARERTLE"])
        self.assertIn("Did you mean", self.sent[0])
        self.assertIn("/news WAAREERTL", self.sent[0])


class RemoveFromWatchlistTests(unittest.TestCase):
    def test_missing_symbol_suggests_the_closest_names_in_the_users_list(self):
        items = [{"symbol": "TCS", "company": "Tata Consultancy Services", "exchange": "NSE"},
                 {"symbol": "INFY", "company": "Infosys", "exchange": "NSE"}]
        sent = []
        with patch.object(watchlist_commands.storage, "get_user_list", return_value=items), \
                patch.object(watchlist_commands.storage, "remove_from_user_list") as remove, \
                patch.object(watchlist_commands, "reply",
                             lambda chat, text, **extra: sent.append(text)):
            watchlist_commands.handle_add_remove("1", ["/removestock", "TCSS"], "/removestock")
        remove.assert_not_called()
        self.assertIn("isn't in your watchlist", sent[0])
        self.assertIn("<code>TCS</code>", sent[0])
        self.assertIn("/removestock TCS", sent[0])

    def test_present_symbol_is_still_removed(self):
        items = [{"symbol": "TCS", "company": "Tata Consultancy Services", "exchange": "NSE"}]
        sent = []
        with patch.object(watchlist_commands.storage, "get_user_list", return_value=items), \
                patch.object(watchlist_commands.storage, "remove_from_user_list") as remove, \
                patch.object(watchlist_commands, "reply",
                             lambda chat, text, **extra: sent.append(text)):
            watchlist_commands.handle_add_remove("1", ["/removestock", "TCS"], "/removestock")
        remove.assert_called_once()
        self.assertIn("Removed", sent[0])


if __name__ == "__main__":
    unittest.main()
