"""Mail-section catalogue + selection helpers (email/sections.py).

These are pure functions - the whole point of putting them in their own module
was so they could be tested without storage, network or Telegram.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from corporate_actions.email import sections as sec  # noqa: E402


class SectionCatalogueTests(unittest.TestCase):
    def test_morning_and_close_catalogues(self):
        self.assertEqual(
            sec.MORNING_SECTIONS,
            ("screener", "wk52", "nifty", "us", "gappers", "watchlist"))
        self.assertEqual(sec.CLOSE_SECTIONS,
                         ("screener", "wk52", "nifty", "us", "eod"))

    def test_every_section_has_a_title(self):
        for kind in ("morning", "close"):
            titles = sec.MORNING_SECTION_TITLES if kind == "morning" else sec.CLOSE_SECTION_TITLES
            for key in sec.default_sections(kind):
                self.assertIn(key, titles, f"{kind}.{key} has no title")

    def test_every_alias_resolves_to_a_real_section(self):
        known = set(sec.MORNING_SECTIONS) | set(sec.CLOSE_SECTIONS)
        for word, key in sec.SECTION_ALIASES.items():
            self.assertIn(key, known, f"alias {word!r} -> unknown section {key!r}")


class NormalizeTests(unittest.TestCase):
    def test_none_and_all_mean_everything(self):
        for raw in (None, "all", "everything", "digest", "full"):
            self.assertEqual(sec.normalize_section_list(raw, "morning")[0],
                             sec.default_sections("morning"))

    def test_alias_words_resolve(self):
        keys, unknown = sec.normalize_section_list("nifty,gaps", "morning")
        self.assertEqual(keys, ["nifty", "gappers"])
        self.assertEqual(unknown, [])

    def test_order_is_always_the_mail_order(self):
        # The user's typing order must not leak into the rendered mail.
        a, _ = sec.normalize_section_list("gappers,nifty", "morning")
        b, _ = sec.normalize_section_list("nifty,gappers", "morning")
        self.assertEqual(a, b)
        self.assertEqual(a, ["nifty", "gappers"])

    def test_duplicates_collapse(self):
        keys, _ = sec.normalize_section_list("nifty,nifty,nifty", "morning")
        self.assertEqual(keys, ["nifty"])

    def test_unknown_words_are_reported_not_dropped(self):
        keys, unknown = sec.normalize_section_list("nifty,foo,bar", "morning")
        self.assertEqual(keys, ["nifty"])
        self.assertEqual(unknown, ["foo", "bar"])

    def test_space_separated_also_works(self):
        keys, _ = sec.normalize_section_list("nifty gappers", "morning")
        self.assertEqual(keys, ["nifty", "gappers"])

    def test_close_mail_has_its_own_catalogue(self):
        keys, unknown = sec.normalize_section_list("eod,nifty", "close")
        self.assertEqual(keys, ["nifty", "eod"])
        self.assertEqual(unknown, [])
        # "watchlist" is a MORNING section - the evening mail must reject it.
        self.assertIsNone(sec.normalize_section_name("watchlist", "close"))


class AddRemoveTests(unittest.TestCase):
    def test_add_appends_in_mail_order(self):
        updated, err = sec.add_section(["nifty"], "gappers", "morning")
        self.assertIsNone(err)
        self.assertEqual(updated, ["nifty", "gappers"])

    def test_add_is_idempotent(self):
        updated, err = sec.add_section(["nifty"], "nifty", "morning")
        self.assertIsNone(err)
        self.assertEqual(updated, ["nifty"])

    def test_remove_keeps_the_rest(self):
        updated, err = sec.remove_section(["nifty", "us", "gappers"], "us", "morning")
        self.assertIsNone(err)
        self.assertEqual(updated, ["nifty", "gappers"])

    def test_unknown_word_errors_and_changes_nothing(self):
        updated, err = sec.add_section(["nifty"], "bogus", "morning")
        self.assertEqual(updated, ["nifty"])
        self.assertIsNotNone(err)


class PreferenceTests(unittest.TestCase):
    def test_no_preference_means_all_sections(self):
        self.assertEqual(sec.sections_for({}, "morning"), sec.default_sections("morning"))
        self.assertEqual(sec.sections_for(None, "close"), sec.default_sections("close"))

    def test_empty_preference_never_silences_the_mail(self):
        # A user clearing the list must not accidentally get an empty mail.
        self.assertEqual(sec.sections_for({"email_sections": []}, "morning"),
                         sec.default_sections("morning"))

    def test_stored_preference_is_honoured_and_ordered(self):
        settings = {"email_sections": ["gappers", "nifty"]}
        self.assertEqual(sec.sections_for(settings, "morning"), ["nifty", "gappers"])

    def test_stale_section_from_a_removed_release_is_dropped(self):
        settings = {"email_sections": ["nifty", "no-longer-exists"]}
        self.assertEqual(sec.sections_for(settings, "morning"), ["nifty"])

    def test_close_preference_uses_its_own_key(self):
        settings = {"email_sections": ["nifty"], "email_close_sections": ["eod"]}
        self.assertEqual(sec.sections_for(settings, "close"), ["eod"])
        self.assertEqual(sec.sections_for(settings, "morning"), ["nifty"])


class DescribeTests(unittest.TestCase):
    def test_human_readable(self):
        self.assertEqual(
            sec.describe_sections(["gappers", "nifty"], "morning"),
            "Gappers, Nifty actions")

    def test_validate_keeps_only_renderable(self):
        self.assertEqual(sec.validate_sections(["us", "nope"], "morning"), ["us"])


if __name__ == "__main__":
    unittest.main()
