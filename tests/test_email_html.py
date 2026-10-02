"""Regression tests: mail HTML keeps its inline-style compatibility contract.

Email clients like Outlook desktop (Word engine) and some gateways strip the
document <style> block or ignore whole class rules - every critical colour,
spacing and layout declaration must therefore be INLINE on the element
(see corporate_actions/email/tables.py and client._html_document).
"""
import unittest

from corporate_actions.email import client
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


if __name__ == "__main__":
    unittest.main()
