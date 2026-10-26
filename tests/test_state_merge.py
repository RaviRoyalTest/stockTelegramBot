"""Pure domain merge regressions: preserve intent, never guess conflicts."""
import json
import unittest

from corporate_actions.state_merge import MISSING, StateConflict, merge_state, strict_json_loads


class StrictJsonTests(unittest.TestCase):
    def test_nested_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        for raw in ('{"chat":{"email":1,"email":2}}', '[NaN]', '[Infinity]',
                    '[-Infinity]', '[1e999]', '[-1e999]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json_loads(raw)

    def test_top_level_duplicate_key_is_rejected(self):
        # object_pairs_hook fires at every nesting level, including the root.
        for raw in ('{"a": 1, "a": 2}', '{"x": null, "x": null}',
                    '{"same": 1, "other": 2, "same": 3}'):
            with self.subTest(raw=raw), self.assertRaises(StateConflict):
                strict_json_loads(raw)

    def test_valid_json_roundtrip(self):
        value = {"a": [1, 1.5, True, None], "b": "text"}
        self.assertEqual(strict_json_loads(json.dumps(value)), value)


class DomainMergeTests(unittest.TestCase):
    def test_settings_merge_independent_recursive_fields_and_deletions(self):
        base = {"chat": {"email": "old", "quiet": False, "nested": {"remove": 1}}}
        local = {"chat": {"email": "new", "quiet": False, "nested": {}}}
        remote = {"chat": {"email": "old", "quiet": True, "nested": {"remove": 1, "add": 2}}}
        self.assertEqual(merge_state("settings", base, local, remote),
                         {"chat": {"email": "new", "quiet": True, "nested": {"add": 2}}})
        self.assertEqual(base["chat"]["nested"], {"remove": 1})

    def test_same_field_divergence_delete_update_and_type_changes_fail(self):
        for base, local, remote in (({"a": 1}, {"a": 2}, {"a": 3}),
                                    ({"a": 1}, {}, {"a": 2}),
                                    ({"a": {}}, {"a": []}, {"a": {"x": 1}}),
                                    ({"a": 0}, {"a": True}, {"a": 1}),
                                    ({"a": [1]}, {"a": [1, 2]}, {"a": [1, 3]})):
            with self.subTest(base=base, local=local, remote=remote), self.assertRaises(StateConflict):
                merge_state("settings", base, local, remote)

    def test_seen_is_monotonic_union_including_pruned_base(self):
        self.assertEqual(merge_state("seen", ["base"], ["local"], ["remote"]),
                         ["base", "local", "remote"])
        self.assertEqual(merge_state("seen", ["base"], MISSING, ["remote"]),
                         ["base", "remote"])
        with self.assertRaises(StateConflict):
            merge_state("seen", [], [1], [])

    def test_stock_adds_and_deletes_by_exchange_and_symbol(self):
        nse = {"exchange": "NSE", "symbol": "ITC"}
        bse = {"exchange": "BSE", "symbol": "ITC"}
        new = {"exchange": "NSE", "symbol": "INFY"}
        result = merge_state("stocks", [nse, bse], [bse], [nse, bse, new])
        self.assertCountEqual(result, [bse, new])

    def test_stock_matching_is_case_insensitive_but_metadata_conflicts_fail(self):
        base = [{"exchange": "NSE", "symbol": "ITC", "name": "old"}]
        local = [{"exchange": "nse", "symbol": "itc", "name": "old"}]
        remote = [{"exchange": "NSE", "symbol": "ITC", "name": "new"}]
        self.assertEqual(merge_state("stocks", base, local, remote),
                         [{"exchange": "nse", "symbol": "itc", "name": "new"}])
        with self.assertRaises(StateConflict):
            merge_state("stocks", base, [], remote)
        with self.assertRaises(StateConflict):
            merge_state("stocks", [], base + base, [])

    def test_subscriptions_merge_each_chat_stock_list(self):
        a = {"exchange": "NSE", "symbol": "A"}
        b = {"exchange": "NSE", "symbol": "B"}
        self.assertEqual(merge_state("subscriptions", {"chat": [a]}, {"chat": []},
                                     {"chat": [a, b], "other": [a]}),
                         {"chat": [b], "other": [a]})

    def test_unknown_structures_are_atomic(self):
        with self.assertRaises(StateConflict):
            merge_state("atomic", {"a": 1}, {"a": 1, "b": 2}, {"a": 1, "c": 3})
        self.assertEqual(merge_state("atomic", {}, {}, {"a": 1}), {"a": 1})
        self.assertIs(merge_state("settings", {}, MISSING, {}), MISSING)


class ReportMergeTests(unittest.TestCase):
    day = "2026-01-05"

    def section(self, market="in", clock="12:00:00+00:00", final=True, count=1):
        # India close=10:00 UTC; US close=21:00 UTC on this date.
        return {"market": market, "session_date": self.day, "mode": "historical",
                "captured_at": f"{self.day}T{clock}", "finalized": final,
                "universes": [{"verified": count, "target": 10, "gainers": [{"symbol": "A"}]}]}

    def doc(self, *sections):
        return {"report": {"sections": list(sections)}}

    def merge(self, base, local, remote):
        return merge_state("report", base, local, remote, session=self.day)

    def test_independent_market_sections_preserve_verified_session_and_totals(self):
        india = self.section()
        us = self.section("us", "22:00:00+00:00", count=2)
        result = self.merge(MISSING, self.doc(india), self.doc(us))
        self.assertEqual(result["report"]["markets"], ["in", "us"])
        self.assertEqual(result["report"]["total_verified"], 3)
        self.assertEqual(result["session_date"], self.day)
        self.assertTrue(result["finalized"])

    def test_newer_verified_capture_wins_as_a_whole(self):
        base = self.doc(self.section(clock="09:00:00+00:00", final=False))
        local = self.doc(self.section(clock="11:00:00+00:00", count=2))
        remote = self.doc(self.section(clock="12:00:00+00:00", count=3))
        result = self.merge(base, local, remote)
        self.assertEqual(result["report"]["sections"][0]["universes"][0]["verified"], 3)

    def test_unverified_identity_time_finality_and_equal_time_conflicts_fail(self):
        good = self.section()
        variants = [{**good, "session_date": "2026-01-06"},
                    {**good, "captured_at": None},
                    {**good, "captured_at": self.day + "T12:00:00"},
                    {**good, "captured_at": self.day + "T09:00:00+00:00"},
                    {**good, "snapshot": {"date": "06-Jan-2026"}},
                    self.section(count=2)]
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(StateConflict):
                self.merge(MISSING, self.doc(good), self.doc(bad))

    def test_finality_regression_and_unsupported_wrapper_conflicts_fail(self):
        with self.assertRaises(StateConflict):
            self.merge(self.doc(self.section(clock="08:00:00+00:00", final=False)),
                       self.doc(self.section()),
                       self.doc(self.section(clock="13:00:00+00:00", final=False)))
        with self.assertRaises(StateConflict):
            self.merge(MISSING, {**self.doc(self.section()), "unsupported": "left"},
                       {**self.doc(self.section("us", "22:00:00+00:00")), "unsupported": "right"})


class ScheduleMergeTests(unittest.TestCase):
    """schedule.json is list-rooted and atomic: no field-level reconciliation."""

    _entry = {"interval_min": 60, "commands": ["/report"], "chat": "123"}
    _other = {"interval_min": 120, "commands": ["/scan"], "chat": "456"}

    def test_identical_schedule_on_all_three_sides_passes(self):
        sched = [self._entry]
        self.assertEqual(merge_state("schedule", sched, sched, sched), sched)

    def test_local_unchanged_takes_remote_change(self):
        base = [self._entry]
        changed = [self._entry, self._other]
        self.assertEqual(merge_state("schedule", base, base, changed), changed)

    def test_remote_unchanged_takes_local_change(self):
        base = [self._entry]
        changed = [self._other]
        self.assertEqual(merge_state("schedule", base, changed, base), changed)

    def test_both_sides_diverge_fails_closed(self):
        # Both writers modified the schedule differently; no field-merge is safe.
        base = [self._entry]
        local = [self._entry, self._other]
        remote = [{**self._entry, "interval_min": 90}]
        with self.assertRaises(StateConflict):
            merge_state("schedule", base, local, remote)

    def test_missing_base_both_sides_identical_passes(self):
        # New file added by both writers with same content.
        sched = [self._entry]
        self.assertEqual(merge_state("schedule", MISSING, sched, sched), sched)

    def test_missing_base_one_side_adds_passes(self):
        # Only one writer added the file; the other side is absent (MISSING).
        sched = [self._entry]
        self.assertEqual(merge_state("schedule", MISSING, sched, MISSING), sched)
        self.assertEqual(merge_state("schedule", MISSING, MISSING, sched), sched)

    def test_missing_base_both_sides_different_fails_closed(self):
        # Two writers independently created different schedule files.
        with self.assertRaises(StateConflict):
            merge_state("schedule", MISSING, [self._entry], [self._other])

    def test_same_field_settings_conflict_is_failclosed(self):
        # Belt-and-suspenders: same scalar changed to two different values.
        with self.assertRaises(StateConflict):
            merge_state("settings", {"a": 1}, {"a": 2}, {"a": 3})
        # Delete-vs-update is equally unresolvable.
        with self.assertRaises(StateConflict):
            merge_state("settings", {"k": 1}, {}, {"k": 2})


if __name__ == "__main__":
    unittest.main()
