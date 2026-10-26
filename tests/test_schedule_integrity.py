"""Scheduler identity, UTC persistence, session gates and IST date regressions."""
import json
import runpy
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from corporate_actions import config, scheduler, storage
from corporate_actions.core import dates
from corporate_actions.market import hours
from corporate_actions.storage import schedule as schedules


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


class NonUtcHostDatetime(datetime):
    """Fail on naive timestamps; simulate a non-UTC host for naive writes."""

    def timestamp(self):
        if self.tzinfo is None:
            raise AssertionError("host-local timestamp conversion")
        return super().timestamp()

    @classmethod
    def fromtimestamp(cls, value, tz=None):
        if tz is None:
            return datetime.fromtimestamp(
                value, timezone(timedelta(hours=-8)),
            ).replace(tzinfo=None)
        return super().fromtimestamp(value, tz)


class ScheduleStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state = root / "schedule.json"
        self.settings = root / "settings.json"
        for name, value in {
            "SCHEDULE_FILE": self.state, "SETTINGS_FILE": self.settings,
            "TELEGRAM_CHAT_ID": "123", "SCHEDULED_REPORTS_CHAT": "",
            "BOT_USERNAME": "example_bot", "SCHEDULED_COMMANDS": ["/scan500"],
            "SCHEDULED_REPORTS_INTERVAL_MIN": 60,
            "SCHEDULED_REPORTS_MARKET": "in",
        }.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def entry(self, **changes):
        result = {"chat": "123", "commands": ["/scan500"], "interval_min": 60,
                  "market": "any"}
        result.update(changes)
        return result

    def test_legacy_naive_and_aware_timestamps_are_utc(self):
        expected = epoch("2026-10-23T10:00:00+00:00")
        with patch.object(dates, "datetime", NonUtcHostDatetime):
            for value in ("2026-10-23T10:00:00", "2026-10-23T10:00:00Z",
                          "2026-10-23T15:30:00+05:30"):
                with self.subTest(value=value):
                    entry = {"next_due": value, "paused_until": value}
                    self.assertEqual(schedules.schedule_next_due_ts(entry), expected)
                    self.assertTrue(hours.entry_paused(entry, expected - 1))
                    self.assertFalse(hours.entry_paused(entry, expected))
                    self.assertEqual(hours.entry_paused_until(entry), "23-Oct 15:30")
        for value in (None, "garbage", "2026-99-01T12:00:00"):
            self.assertIsNone(schedules.schedule_next_due_ts({"next_due": value}))
            self.assertFalse(hours.entry_paused({"paused_until": value}))

    def test_new_due_and_pause_writes_are_aware_utc(self):
        schedules.save_schedule([self.entry(), self.entry(chat="456")])
        due = epoch("2026-10-23T10:00:00+00:00")
        with patch.object(schedules, "datetime", NonUtcHostDatetime):
            schedules.set_schedule_next_due("123", ["/scan500"], 60, due)
            schedules.pause_schedule("123", due)
        rows = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["next_due"], "2026-10-23T10:00:00+00:00")
        self.assertEqual(rows[0]["paused_until"], rows[0]["next_due"])
        self.assertNotIn("paused_until", rows[1])

    def test_full_identity_selects_only_the_right_row(self):
        base = self.entry(run_at="09:15", timezone="Asia/Kolkata")
        variants = [{"interval_min": 30}, {"run_at": "15:30"},
                    {"at_times": ["10:00", "15:30"]},
                    {"timezone": "America/New_York"}, {"market": "us"},
                    {"window_start": "09:15", "window_end": "15:30"}]
        rows = [base] + [dict(base, **change) for change in variants]
        schedules.save_schedule(rows)
        identities = [schedules.schedule_identity(row) for row in rows]
        self.assertEqual(len(set(identities)), len(rows))
        for index, row in enumerate(rows):
            schedules.set_schedule_next_due(
                row["chat"], row["commands"], row["interval_min"], 1000 + index,
                run_at=row.get("run_at"), at_times=row.get("at_times"),
                timezone=row.get("timezone"), market=row.get("market"),
                window_start=row.get("window_start"), window_end=row.get("window_end"),
            )
        loaded = schedules.load_schedule()
        self.assertEqual([schedules.schedule_next_due_ts(row) for row in loaded],
                         [1000 + index for index in range(len(rows))])
        self.assertEqual([schedules.schedule_identity(row) for row in loaded], identities)

    def test_old_update_call_does_not_guess_between_ambiguous_rows(self):
        schedules.save_schedule([self.entry(run_at="09:15"), self.entry(run_at="15:30")])
        schedules.set_schedule_next_due("123", ["/scan500"], 60, 1000)
        self.assertTrue(all(row["next_due"] is None for row in schedules.load_schedule()))

    def test_legacy_empty_chat_and_command_whitespace_match(self):
        schedules.save_schedule([self.entry(chat="", commands=[" /scan500 "])])
        schedules.set_schedule_next_due("123", ["/scan500"], 60, 1000)
        self.assertEqual(schedules.schedule_next_due_ts(schedules.load_schedule()[0]), 1000)

    def test_canonical_times_share_identity(self):
        self.assertEqual(
            schedules.schedule_identity(self.entry(run_at="15:30,9:15,09:15")),
            schedules.schedule_identity(self.entry(at_times=["09:15", "15:30"])),
        )

    def test_clear_disables_defaults_and_preserves_other_settings_and_chats(self):
        storage.save_user_settings("123", {"schedule_market": "us", "email": "a@example.com"})
        schedules.save_schedule([self.entry(), self.entry(chat="456")])
        schedules.clear_schedule("123")
        self.assertFalse(schedules.schedule_defaults_enabled("123"))
        self.assertEqual(storage.get_user_settings("123"), {
            "schedule_market": "us", "email": "a@example.com",
            "schedule_defaults_disabled": True,
        })
        self.assertEqual([row["chat"] for row in scheduler.schedule_entries_with_defaults("123")],
                         ["456"])
        schedules.add_schedule_entry(30, ["/scan500"], "123")
        self.assertEqual(len(scheduler.schedule_entries_with_defaults("123")), 2)

    def test_clear_env_only_and_remove_last_do_not_reactivate_defaults(self):
        self.assertTrue(scheduler.schedule_entries_with_defaults("123")[0]["default"])
        schedules.clear_schedule("123")
        self.assertEqual(scheduler.schedule_entries_with_defaults("123"), [])
        schedules.add_schedule_entry(60, ["/scan500"], "123")
        schedules.remove_schedule_entry("123", 0)
        self.assertEqual(scheduler.schedule_entries_with_defaults("123"), [])

    def test_removing_last_legacy_owner_row_disables_defaults(self):
        schedules.save_schedule([self.entry(chat=""), self.entry(chat="456")])
        schedules.remove_schedule_entry("123", 0)
        self.assertFalse(schedules.schedule_defaults_enabled("123"))
        self.assertEqual(len(scheduler.schedule_entries_with_defaults("123")), 1)

    def test_subscriber_clear_does_not_disable_owner_defaults(self):
        schedules.clear_schedule("456")
        self.assertTrue(scheduler.schedule_entries_with_defaults("123")[0]["default"])

    def test_owner_clear_disables_separate_env_delivery_target(self):
        with patch.object(config, "SCHEDULED_REPORTS_CHAT", "-999"):
            schedules.clear_schedule("123")
            self.assertEqual(scheduler.schedule_entries_with_defaults("-999"), [])

    def test_manual_run_honors_disabled_defaults(self):
        from corporate_actions.bot import schedule_commands as commands
        schedules.clear_schedule("123")
        with patch.object(commands, "reply") as reply, \
             patch.object(commands, "run_command_sequence") as run:
            commands.run_schedule_now("123")
        run.assert_not_called()
        self.assertIn("No scheduled commands", reply.call_args.args[1])

    def test_runtime_timers_are_independent_for_same_commands(self):
        now = epoch("2026-10-23T10:00:00+00:00")
        rows = [self.entry(interval_min=15, next_due="2026-10-23T11:00:00Z"),
                self.entry(interval_min=30, next_due="2026-10-23T09:59:00Z")]
        schedules.save_schedule(rows)

        class StopLoop(BaseException):
            pass

        with patch.object(config, "SCHEDULED_REPORTS_ENABLED", True), \
             patch.object(config, "PROCESS_COMMANDS", True), \
             patch.object(scheduler.threading, "Thread") as thread, \
             patch.object(scheduler.time, "time", return_value=now), \
             patch.object(scheduler.time, "sleep", side_effect=StopLoop), \
             patch.object(scheduler.storage, "is_quiet", return_value=False), \
             patch.object(scheduler, "schedule_source_label", return_value="source"):
            from unittest.mock import Mock
            run = Mock()
            scheduler.start_scheduled_reports(run)
            with self.assertRaises(StopLoop):
                thread.call_args.kwargs["target"]()
        run.assert_called_once_with("123", "/scan500", "source")
        loaded = schedules.load_schedule()
        self.assertEqual(loaded[0]["next_due"], rows[0]["next_due"])
        self.assertEqual(schedules.schedule_next_due_ts(loaded[1]), now + 30 * 60)


class CalendarAndDateTests(unittest.TestCase):
    def test_weekends_block_grace_but_weekday_close_still_fires(self):
        for market, close, offset in (("in", "15:30", "+05:30"),
                                      ("us", "16:00", "-04:00")):
            entry = {"market": market, "run_at": close, "interval_min": 1440}
            for day, allowed in (("2026-10-23", True), ("2026-10-24", False),
                                 ("2026-10-25", False)):
                due = epoch(f"{day}T{close}:00{offset}")
                with self.subTest(market=market, day=day):
                    self.assertEqual(scheduler._gate_allows(entry, market, due + 60, due, 1440),
                                     allowed)
                    self.assertFalse(scheduler._gate_allows(entry, market, due + 601, due, 1440))

    def test_any_market_allows_weekend_explicit_window(self):
        entry = {"market": "any", "window_start": "09:00", "window_end": "10:00"}
        due = epoch("2026-10-24T10:00:00+05:30")
        self.assertTrue(scheduler._gate_allows(entry, "any", due, due, 60))

    def test_weekend_anchors_skip_to_monday(self):
        entry = {"market": "in", "interval_min": 15}
        after = epoch("2026-10-23T15:31:00+05:30")
        self.assertEqual(scheduler._first_due(entry, "in", 15, after),
                         epoch("2026-10-26T09:15:00+05:30"))
        self.assertEqual(hours.next_open_after("in", after),
                         epoch("2026-10-26T09:15:00+05:30"))

    def test_explicit_timezone_applies_to_anchor_and_window(self):
        entry = {"market": "any", "at_times": ["09:30"],
                 "timezone": "America/New_York"}
        after = epoch("2026-07-01T10:00:00Z")
        self.assertEqual(scheduler._first_due(entry, "any", 1440, after),
                         epoch("2026-07-01T13:30:00Z"))
        entry.update(window_start="09:30", window_end="10:30")
        self.assertTrue(hours.entry_in_window(entry, now=epoch("2026-07-01T14:00:00Z")))

    def test_new_york_clock_uses_dst(self):
        for month, utc_hour in ((1, 14), (7, 13)):
            after = datetime(2026, month, 5, 0, tzinfo=timezone.utc).timestamp()
            expected = datetime(2026, month, 5, utc_hour, 30, tzinfo=timezone.utc).timestamp()
            self.assertEqual(dates.next_at_in_tz_after("09:30", "America/New_York", after),
                             expected)

    def test_ist_fixed_fallback_when_zoneinfo_data_is_missing(self):
        with patch("zoneinfo.ZoneInfo", side_effect=KeyError("no tzdata")):
            namespace = runpy.run_path(str(Path(dates.__file__)))
        self.assertEqual(namespace["IST"].utcoffset(None), timedelta(hours=5, minutes=30))
        self.assertIsNotNone(namespace["now_ist"]().tzinfo)
        self.assertEqual(namespace["next_at_in_tz_after"]("09:15", None, 0), 13500)
        with patch("zoneinfo.ZoneInfo", side_effect=KeyError("no tzdata")):
            with self.assertRaises(KeyError):
                namespace["timezone_for"]("America/New_York")

    def test_default_year_and_session_estimate_use_ist_today(self):
        with patch.object(dates, "today_ist", return_value=date(2031, 1, 1)):
            self.assertEqual(dates.parse_date_token("12aug"), date(2031, 8, 12))
            self.assertEqual(dates.date_from_parts(["12", "August"], 0),
                             (date(2031, 8, 12), 2))
            self.assertEqual(dates.sessions_back_estimate(date(2030, 12, 25)), 9)
        self.assertEqual(dates.format_timestamp(epoch("2026-12-31T20:00:00Z")),
                         "01-Jan 01:30")


if __name__ == "__main__":
    unittest.main()
