"""Regression tests: mail HTML keeps its inline-style compatibility contract.

Email clients like Outlook desktop (Word engine) and some gateways strip the
document <style> block or ignore whole class rules - every critical colour,
spacing and layout declaration must therefore be INLINE on the element
(see corporate_actions/email/tables.py and client._html_document).
"""
import unittest

from corporate_actions.email import client, theme
from corporate_actions.email.tables import (
    actions_table,
    index_table,
    kv_table,
    move_cell,
    muted,
    section,
    stat_chips,
    stock_table,
    watchlist_table,
)


class MoveCellTests(unittest.TestCase):
    def test_positive_change_is_inline_green(self):
        html = move_cell(2.5)
        self.assertIn("color:#059669", html)
        self.assertIn("font-weight:700", html)
        self.assertIn("+2.50%", html)

    def test_negative_change_is_inline_red(self):
        html = move_cell(-3.1)
        self.assertIn("color:#e11d48", html)
        self.assertIn("-3.10%", html)

    def test_non_numeric_renders_dash(self):
        self.assertEqual(move_cell(None), "-")
        self.assertEqual(move_cell("n/a"), "-")


class InlineStyleContractTests(unittest.TestCase):
    def test_stat_chips_have_inline_box(self):
        html = stat_chips([("Verified", "20/20")])
        self.assertIn("display:inline-block", html)
        self.assertIn("background-color:#f1f5f9", html)
        self.assertIn("border-radius:10px", html)

    def test_muted_paragraph_has_inline_style(self):
        html = muted("hello")
        self.assertIn('class="muted"', html)
        self.assertIn("color:#64748b", html)
        self.assertIn("font-size:12px", html)

    def test_section_accent_is_inline(self):
        html = section("Opening session", "green", "🌅")
        self.assertIn("border-left:4px solid #10b981", html)
        self.assertIn("background-color:#f8fafc", html)

    def test_stock_table_inline_collapse_and_zebra(self):
        rows = [
            {"symbol": "AAA", "name": "Alpha", "price": 100, "change_pct": 1.5,
             "volume": 1000, "volume_change_pct": 2},
            {"symbol": "BBB", "name": "Beta", "price": 50, "change_pct": -1,
             "volume": 500, "volume_change_pct": -2},
            {"symbol": "CCC", "name": "Gamma", "price": 10, "change_pct": 0},
        ]
        html = stock_table(rows)
        self.assertIn("border-collapse:collapse", html)
        # Row 2 carries the zebra tint, inline on its cells.
        self.assertIn("background-color:#f8fafc", html)
        self.assertIn("rs-table", html)

    def test_empty_stock_table_degrades_to_muted_line(self):
        self.assertIn("muted", stock_table([]))

    def test_watchlist_pill_background_inline(self):
        up = watchlist_table([{"symbol": "AAA", "change_pct": 3}])
        down = watchlist_table([{"symbol": "BBB", "change_pct": -3}])
        self.assertIn("background-color:#dcfce7", up)
        self.assertIn("background-color:#ffe4e6", down)

    def test_index_and_actions_tables_are_inline_styled(self):
        self.assertIn("border-collapse:collapse", index_table(
            [{"label": "NIFTY", "level": 25000, "change_pct": 0.4}]))
        self.assertIn("border-collapse:collapse", actions_table(
            [{"symbol": "X", "action": "DIV", "ex_date": "2026-10-05"}]))
        self.assertIn("border-collapse:collapse", kv_table([("K", "v")]))


class HtmlDocumentTests(unittest.TestCase):
    def setUp(self):
        self.doc = client._html_document(
            "Royal Stock close + EOD: 2026-10-02",
            [section("Opening session screener", "green", "🌅"),
             stock_table([{"symbol": "AAA", "price": 1, "change_pct": 1}])])

    def test_gradient_bar_has_solid_fallback(self):
        # background-image keeps the gradient, background-color survives in
        # gradient-ignorant clients (Outlook desktop) - never the bare
        # `background:` shorthand, which would reset the fallback colour.
        self.assertIn("background-color:#6366f1", self.doc)
        self.assertIn("background-image:linear-gradient(90deg", self.doc)
        self.assertNotIn("background:linear-gradient", self.doc)

    def test_document_keeps_accessibility_and_preheader(self):
        self.assertIn('role="presentation"', self.doc)
        self.assertIn("display:none", self.doc)  # hidden preheader text

    def test_body_lines_render_inline_inside_card(self):
        self.assertIn("border-left:4px solid #10b981", self.doc)
        self.assertIn("color:#059669", self.doc)  # inline green move badge


class DarkModeTests(unittest.TestCase):
    """Progressive dark theme: <style> overlay, inline palette untouched."""

    def setUp(self):
        self.doc = client._html_document(
            "Royal Stock close + EOD: 2026-10-02",
            [section("Opening session screener", "green", "🌅"),
             stock_table([{"symbol": "AAA", "price": 1, "change_pct": 1}]),
             watchlist_table([{"symbol": "BBB", "change_pct": -3}]),
             stat_chips([("Verified", "20/20")])])

    def test_style_block_carries_dark_overlay(self):
        self.assertIn("@media (prefers-color-scheme:dark)", self.doc)
        body = self.doc.split("@media (prefers-color-scheme:dark)", 1)[1]
        # The overlay drives the dark palette tokens.
        self.assertIn(theme.D_PAGE_BG.lstrip("#").upper(), body.upper())
        self.assertIn(theme.D_CARD_BG.lstrip("#").upper(), body.upper())
        self.assertIn(".rs-table th", body)
        self.assertIn(".pill.pos", body)

    def test_light_palette_inside_inline_styles_is_untouched(self):
        # The dark overlay lives ONLY in <style>; inline styles keep the
        # fixed light values - style-stripping clients keep the light look.
        for colour in (theme.PAGE_BG, theme.CARD_BG, theme.INK, theme.TITLE_FG):
            self.assertIn(colour, self.doc)
        self.assertNotIn("style=\"background-color:" + theme.D_PAGE_BG,
                         self.doc)

    def test_outlook_static_guard_signature_present(self):
        # Outlook's engine needs the preferred-scheme + :root declarations
        # in the <style> block to disable auto dark-tone inverting.
        style = self.doc.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn(":root{color-scheme:light dark", style)
        self.assertIn("prefers-color-scheme:dark", style)

    def test_no_color_scheme_light_meta(self):
        # A meta declaring light-only would pin the whole document light.
        self.assertNotIn("supported-color-schemes\" content=\"light\"", self.doc)
        self.assertNotIn("color-scheme\" content=\"light\"", self.doc)

    def test_dark_declarations_are_important(self):
        # Inline styles beat normal selectors; !important is required.
        body = self.doc.split("@media (prefers-color-scheme:dark)", 1)[1]
        self.assertGreaterEqual(body.count("!important"), 10)

    def test_dark_rules_are_complete_and_valid_css(self):
        # Regression: _imp() must emit full `{prop:value !important;}` rules
        # (no missing braces) with hyphenated property names only -
        # background_color (underscore) is invalid CSS and silently drops.
        style = self.doc.split("<style>", 1)[1].split("</style>", 1)[0]
        # Split after '@media ...{' but account for that consumed '{' by
        # counting the block's own final '}}' (last rule + media query).
        prefix, dark = style.split("prefers-color-scheme:dark){", 1)
        depth = 0
        for index, char in enumerate(dark):
            if char == "{":
                self.assertEqual(
                    depth, 0, f"unclosed rule before new {{ at {index}")
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:  # only the media query's own closer does this
                    self.assertGreaterEqual(
                        index, len(dark.rstrip()) - 2,
                        "media query closed early")
                    break
        # Final depth is -1: the split point consumed the media query's own
        # opening '{', whose matching closer is the block's final '}'.
        self.assertEqual(depth, -1)
        self.assertNotIn("_important", dark)  # property underscores leaked
        self.assertIn("body{background-color:", dark)
        self.assertNotIn("background_color", dark)

    def test_hook_attributes_present(self):
        # data-rs-* hooks let the overlay reach card/brand/footer chrome.
        for hook in ("data-rs-card", "data-rs-brand", "data-rs-title",
                     "data-rs-subtitle", "data-rs-footer"):
            self.assertIn(hook, self.doc)

    def test_theme_light_palette_matches_tables_and_client(self):
        # Single source of truth: every value embedded in tables.py's
        # inline styles comes from theme.py (spot-check via public builders).
        html = section("x", "green")
        self.assertIn(theme.SECTION_BG, html)
        self.assertIn(theme.ACCENT_GREEN, html)
        self.assertIn(theme.TITLE_FG, html)


if __name__ == "__main__":
    unittest.main()
