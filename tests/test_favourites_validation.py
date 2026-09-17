"""Guards for /myfavourites set|add and /schedule add (no garbage saved, no loops)."""
import unittest

from corporate_actions.bot.registry import is_known_command, normalize_command
from corporate_actions.bot.watchlist_commands import (
    _group_favourite_commands,
    _validate_favourite_commands,
)


class GroupingTests(unittest.TestCase):
    def test_normal_grouping(self):
        self.assertEqual(
            _group_favourite_commands(["/toplosers", "1h", "/news"]),
            ["/toplosers 1h", "/news"],
        )

    def test_double_slash_token_collapses(self):
        # '//toplosers 1h /news' was almost certainly '/toplosers 1h /news'
        self.assertEqual(
            _group_favourite_commands(["//toplosers", "1h", "/news"]),
            ["/toplosers 1h", "/news"],
        )

    def test_leading_double_slash_single_command(self):
        self.assertEqual(
            _group_favourite_commands(["//toplosers", "1h"]),
            ["/toplosers 1h"],
        )


class ValidationTests(unittest.TestCase):
    def test_placeholder_cmd_rejected(self):
        valid, problems = _validate_favourite_commands(["/cmd"])
        self.assertEqual(valid, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("placeholder", problems[0])

    def test_self_reference_rejected(self):
        valid, problems = _validate_favourite_commands(["/myfavourites run"])
        self.assertEqual(valid, [])
        self.assertIn("never stop", problems[0])

    def test_unknown_command_rejected(self):
        valid, problems = _validate_favourite_commands(["/totallybogus 5"])
        self.assertEqual(valid, [])
        self.assertIn("unknown command", problems[0])

    def test_valid_command_passes(self):
        valid, problems = _validate_favourite_commands(["/toplosers 1h", "/news"])
        self.assertEqual(valid, ["/toplosers 1h", "/news"])
        self.assertEqual(problems, [])

    def test_mixed_reports_each(self):
        valid, problems = _validate_favourite_commands(
            ["/toplosers 1h", "/cmd", "/nope", "/myfavourites"]
        )
        self.assertEqual(valid, ["/toplosers 1h"])
        self.assertEqual(len(problems), 3)


class RegistryTests(unittest.TestCase):
    def test_normalize_collapses_double_slash(self):
        self.assertEqual(normalize_command("//myfavourites"), "/myfavourites")
        self.assertEqual(normalize_command("///help"), "/help")

    def test_normalize_strips_bot_mention(self):
        self.assertEqual(normalize_command("/watchlist@StockVigilBot"), "/watchlist")

    def test_normalize_maps_aliases(self):
        self.assertEqual(normalize_command("/favorites"), "/myfavourites")
        self.assertEqual(normalize_command("/gap"), "/gappers")

    def test_unknown_is_none(self):
        self.assertIsNone(normalize_command("/cmd"))
        self.assertIsNone(normalize_command("hello"))

    def test_is_known_command(self):
        self.assertTrue(is_known_command("/toplosers 1h"))
        self.assertTrue(is_known_command("/scan500"))  # menu command
        self.assertFalse(is_known_command("/cmd"))
        self.assertFalse(is_known_command("//nope"))


if __name__ == "__main__":
    unittest.main()
