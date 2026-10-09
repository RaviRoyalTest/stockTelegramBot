import unittest
from datetime import date, timedelta
from unittest.mock import patch

from corporate_actions import storage as storage_mod
from corporate_actions.email import daily as daily_mod
from corporate_actions.email.daily import build_combined_lines, build_daily_lines
from corporate_actions.email.daily import build_eod_store_lines, build_full_session_lines
from corporate_actions.email.daily import build_open_lines, get_scope
from corporate_actions.email.daily import maybe_send_eod_email, maybe_send_open_email


class DailyEmailTests(unittest.TestCase):
    def test_build_daily_lines_sections(self):
        snapshot = {
            "session": "2026-09-25",
            "universe": "nifty500",
            "gap_downs": [{"symbol": "A", "gap_pct": -3.5, "price": 100.0}],
            "top_gainers": [{"symbol": "B", "change_pct": 5.0, "price": 50.0}],
            "top_losers": [],
            "corporate_actions": [
                {"symbol": "C", "action": "Dividend", "ex_date": "2026-09-30"}
            ],
        }
        text = "\n".join(build_daily_lines(snapshot))
        self.assertIn("2026-09-25", text)
        self.assertIn("gap-downs", text)
        self.assertIn("Session movers", text)
        self.assertIn("Corporate actions", text)
        self.assertIn("/dailyemail off", text)

    def test_build_daily_lines_empty_snapshot(self):
        lines = build_daily_lines({})
        self.assertTrue(any("Daily market digest" in line for line in lines))

    def test_open_lines_colorful_tables(self):
        report = {
            "total_verified": 20, "total_target": 20,
            "sections": [{
                "market": "in",
                "snapshot": {"date": "01-Oct-2026", "time_local": "09:15", "state": "OPEN"},
                "universes": [{
                    "title": "NIFTY 100", "verified": 20, "target": 20,
                    "gainers": [{"symbol": "RELIANCE", "name": "Reliance",
                                 "price": 2500.5, "change_pct": 2.5,
                                 "volume": 1000000, "volume_change_pct": 60.0}],
                    "losers": [{"symbol": "TCS", "name": "TCS",
                                "price": 3000.0, "change_pct": -1.2,
                                "volume": 500000, "volume_change_pct": -10.0}],
                }],
                "indices": [{"label": "NIFTY 50", "level": 25000, "change_pct": 0.5}],
            }],
        }
        text = "\n".join(build_open_lines(report, "01-Oct-2026"))
        self.assertIn("RELIANCE", text)
        self.assertIn("rs-table", text)
        self.assertIn("pos", text)
        self.assertIn("Opening session screener", text)

    def test_scope_mapping(self):
        self.assertEqual(get_scope({"daily_email": True}), "both")
        self.assertEqual(get_scope({"daily_email": True, "email_scope": "open"}), "open")
        self.assertEqual(get_scope({"daily_email": True, "email_scope": "close"}), "close")
        self.assertEqual(get_scope({"daily_email": False}), "off")

    def test_full_session_single_banner(self):
        report = {
            "total_verified": 10, "total_target": 20,
            "sections": [{
                "market": "in",
                "snapshot": {"date": "01-Oct-2026", "time_local": "15:30", "state": "CLOSED"},
                "universes": [{
                    "title": "NIFTY 100", "verified": 10, "target": 20,
                    "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
                    "losers": [],
                }],
                "indices": [],
            }],
        }
        text = "\n".join(build_full_session_lines(report, "01-Oct-2026"))
        self.assertIn("Open + Close session screener", text)
        self.assertIn("RELIANCE", text)
        self.assertEqual(text.count("Open + Close session screener"), 1)

    def test_combined_lines_session_plus_stores(self):
        report = {
            "total_verified": 10, "total_target": 20,
            "sections": [{
                "market": "in",
                "snapshot": {"date": "01-Oct-2026", "time_local": "15:30", "state": "CLOSED"},
                "universes": [{
                    "title": "NIFTY 100", "verified": 10, "target": 20,
                    "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
                    "losers": [],
                }],
                "indices": [],
            }],
        }
        with patch("corporate_actions.email.daily.storage") as store:
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = []
            store.load_schedule_for.return_value = []
            store.load_snapshots.return_value = {}
            store.list_snapshot_dates.return_value = []
            store.list_openclose_dates.return_value = []
            text = "\n".join(build_combined_lines("123", report, "01-Oct-2026", []))
        self.assertIn("Open + Close session screener", text)
        self.assertIn("stored details", text)
        self.assertIn("RELIANCE", text)

    def test_eod_store_tables(self):
        with patch("corporate_actions.email.daily.storage") as store, \
                patch("corporate_actions.poller.fetch_matching", return_value=[]):
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = [{"symbol": "RELIANCE", "company": "Reliance", "exchange": "NSE"}]
            store.load_schedule_for.return_value = [{"interval_min": 180, "commands": ["/scan500"]}]
            store.load_snapshots.return_value = {}
            store.list_snapshot_dates.return_value = ["2026-10-01"]
            store.list_openclose_dates.return_value = ["2026-10-01"]
            quotes = [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.5}]
            text = "\n".join(build_eod_store_lines("123", quotes))
        self.assertIn("RELIANCE", text)
        self.assertIn("rs-table", text)
        self.assertIn("Watchlist", text)


def _live_report(label="2099-01-05"):
    return {
        "sections": [{
            "market": "in",
            "snapshot": {"date": label, "time_local": "10:00", "state": "OPEN"},
            "universes": [{
                "title": "NIFTY 100", "verified": 10, "target": 20,
                "gainers": [{"symbol": "RELIANCE", "price": 100.0, "change_pct": 1.0}],
                "losers": [],
            }],
            "indices": [],
        }],
        "total_verified": 10, "total_target": 20,
    }


class MarketGatedMailTests(unittest.TestCase):
    """No automatic mail when the market is closed; the morning mail builds
    a live opening scan when the recorded file is stale."""

    def setUp(self):
        daily_mod._TRADED_DAY_CACHE.clear()
        self.settings = {"email": "a@b.com", "daily_email": True, "email_scope": "both"}
        patches = [
            patch.object(daily_mod, "in_open_window", return_value=True),
            patch.object(daily_mod, "in_close_window", return_value=True),
            patch.object(daily_mod, "email_configured", return_value=True),
            patch.object(storage_mod, "get_user_settings", return_value=dict(self.settings)),
            patch.object(storage_mod, "save_user_settings"),
            # Live action/gap fetches stay hermetic (no network in unit tests).
            patch("corporate_actions.poller.fetch_matching", return_value=[]),
            patch.object(daily_mod, "fetch_nifty_actions", return_value=[]),
            patch.object(daily_mod, "fetch_morning_gaps", return_value=([], [])),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        sender = patch.object(daily_mod, "send_email", return_value=(True, ""))
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def test_open_skipped_when_market_closed(self):
        with patch("corporate_actions.market.hours.is_market_open", return_value=False):
            self.assertFalse(maybe_send_open_email("123"))
        self.send.assert_not_called()

    def test_open_builds_live_when_recorded_stale(self):
        stale = {"recorded_at": "2020-01-01T00:00:00+00:00",
                 "report": _live_report("old")}
        live = _live_report()
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose", return_value=stale), \
                patch("corporate_actions.opening_report.report.collect", return_value=live) as collect, \
                patch("corporate_actions.opening_report.report.save_openclose_doc") as save:
            self.assertTrue(maybe_send_open_email("123"))
        collect.assert_called_once()
        save.assert_called_once()
        subject = self.send.call_args[0][1]
        self.assertIn("opening", subject)

    def _us_only_doc(self, stamped):
        return {
            "recorded_at": stamped, "markets": ["us"],
            "report": {
                "sections": [{
                    "market": "us",
                    "snapshot": {"date": "x", "time_local": "09:41", "state": "OPEN"},
                    "universes": [{
                        "title": "MEGA CAP ($200B+)", "verified": 0, "target": 20,
                        "gainers": [], "losers": [],
                    }],
                    "indices": [],
                }],
                "total_verified": 0, "total_target": 20,
            },
        }

    def test_buy_by_column_and_dates(self):
        from corporate_actions.email.tables import actions_table, buy_by_date

        self.assertEqual(buy_by_date("2026-10-10"), "09-Oct")
        self.assertEqual(buy_by_date(""), "")
        self.assertEqual(buy_by_date("not-a-date"), "")
        plain = actions_table([{"symbol": "A", "subject": "Div", "ex_date": "2026-10-10"}])
        self.assertNotIn("Buy by", plain)
        rich = actions_table(
            [{"symbol": "A", "subject": "Div", "ex_date": "2026-10-10"}], buy_by=True)
        self.assertIn("Buy by", rich)
        self.assertIn("09-Oct", rich)

    def test_split_gaps_sorts_extremes_first(self):
        from corporate_actions.email.daily import split_gaps

        rows = [
            {"symbol": "A", "gap_pct": -1.0},
            {"symbol": "B", "gap_pct": -5.0},
            {"symbol": "C", "gap_pct": 3.0},
            {"symbol": "D", "gap_pct": 0.0},
            {"symbol": "E", "gap_pct": None},
        ]
        downs, ups = split_gaps(rows)
        self.assertEqual([r["symbol"] for r in downs], ["B", "A"])
        self.assertEqual([r["symbol"] for r in ups], ["C"])

    def test_prev_and_us_report_helpers(self):
        from corporate_actions.email import daily as dm

        prev_sections = [{
            "market": "in",
            "snapshot": {"date": "old", "time_local": "", "state": ""},
            "universes": [{"key": "k", "title": "T", "verified": 1, "target": 20,
                           "gainers": [], "losers": []}],
            "indices": [],
        }]
        us_sections = [{
            "market": "us",
            "snapshot": {"date": "old", "time_local": "", "state": ""},
            "universes": [{"key": "u", "title": "U", "verified": 2, "target": 20,
                           "gainers": [], "losers": []}],
            "indices": [],
        }]
        docs = {
            "2099-01-06": {"report": {"sections": prev_sections,
                                      "total_verified": 1, "total_target": 20}},
            "2099-01-05": {"report": {"sections": us_sections,
                                      "total_verified": 2, "total_target": 20}},
        }
        with patch.object(storage_mod, "list_openclose_dates",
                          return_value=["2099-01-05", "2099-01-06"]), \
                patch.object(storage_mod, "load_openclose",
                             side_effect=lambda day=None: docs.get(day, {})):
            prev, prev_label = dm._latest_recorded_before("2099-01-07")
            self.assertEqual(prev_label, "2099-01-06")
            self.assertTrue(prev.get("sections"))
            sub, _label = dm._latest_us_report()
            self.assertEqual([s["market"] for s in sub["sections"]], ["us"])
            self.assertEqual((sub["total_verified"], sub["total_target"]), (2, 20))

    def test_open_mail_includes_all_stocks_action_summary(self):
        """The morning mail carries the watchlist + Nifty action blocks."""
        from corporate_actions.core.dates import today_ist

        stamped = f"{today_ist().isoformat()}T10:00:00+05:30"
        live = _live_report()
        watch_actions = [{
            "symbol": "ACT", "subject": "Dividend Rs 5", "exchange": "NSE",
            "ex_date": today_ist().isoformat(),
        }]
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose",
                             return_value={"recorded_at": stamped, "report": live}), \
                patch("corporate_actions.poller.fetch_matching",
                      return_value=watch_actions), \
                patch.object(daily_mod, "fetch_nifty_actions",
                             return_value=watch_actions):
            self.assertTrue(maybe_send_open_email("123"))
        body = "\n".join(self.send.call_args[0][2])
        self.assertIn("your full watchlist", body)
        self.assertIn("Nifty 500", body)
        self.assertIn("ACT", body)

    def test_morning_mail_has_all_five_sections(self):
        """Open + last close + Nifty(buy-by) + U.S. + gappers + watchlist."""
        from datetime import date, timedelta

        from corporate_actions.core.dates import today_ist
        from corporate_actions.email import daily as dm

        today = today_ist().isoformat()
        yesterday = (today_ist() - timedelta(days=1)).isoformat()
        stamped = f"{today}T10:00:00+05:30"
        live = _live_report()
        prev_sections = [{
            "market": "in",
            "snapshot": {"date": "old", "time_local": "", "state": ""},
            "universes": [{"key": "k", "title": "T", "verified": 1, "target": 20,
                           "gainers": [], "losers": []}],
            "indices": []}]
        us_sections = [{
            "market": "us",
            "snapshot": {"date": "old", "time_local": "", "state": ""},
            "universes": [{"key": "u", "title": "U", "verified": 2, "target": 20,
                           "gainers": [], "losers": []}],
            "indices": []}]
        docs = {
            None: {"recorded_at": stamped, "report": live},
            today: {"recorded_at": stamped, "report": live},
            yesterday: {"recorded_at": f"{yesterday}T10:00:00+05:30",
                        "report": {"sections": prev_sections + us_sections,
                                   "total_verified": 3, "total_target": 40}},
        }
        future = (date.today() + timedelta(days=3)).isoformat()
        actions = [{"symbol": "ACT", "subject": "Dividend Rs 5",
                    "exchange": "NSE", "ex_date": future}]
        gaps = ([{"symbol": "GD", "name": "G Down", "open": 100.0,
                  "gap_pct": -2.0, "move_from_open_pct": 0.5}], [])
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose",
                             side_effect=lambda day=None: docs.get(day, {})), \
                patch.object(storage_mod, "list_openclose_dates",
                             return_value=[yesterday, today]), \
                patch.object(dm, "fetch_watchlist_actions", return_value=actions), \
                patch.object(dm, "fetch_nifty_actions", return_value=actions), \
                patch.object(dm, "fetch_morning_gaps", return_value=gaps), \
                patch.object(storage_mod, "load_snapshots", return_value={}):
            self.assertTrue(maybe_send_open_email("123"))
        body = "\n".join(self.send.call_args[0][2])
        self.assertIn("Opening session screener", body)
        self.assertIn("(last close)", body)
        self.assertIn("Buy by", body)
        self.assertIn("U.S. market", body)
        self.assertIn("Overnight gaps", body)
        self.assertIn("your full watchlist", body)

    def test_morning_close_and_us_recaps(self):
        from corporate_actions.email import daily as dm

        prev = {"sections": [{
            "market": "in",
            "snapshot": {"date": "old", "time_local": "", "state": ""},
            "universes": [{"key": "k", "title": "T", "verified": 1, "target": 20,
                           "gainers": [], "losers": []}],
            "indices": []}],
            "total_verified": 1, "total_target": 20}
        us_doc = {"report": {"sections": [{
            "market": "us",
            "snapshot": {"date": "old", "time_local": "", "state": ""},
            "universes": [{"key": "u", "title": "U", "verified": 2, "target": 20,
                           "gainers": [], "losers": []}],
            "indices": []}],
            "total_verified": 2, "total_target": 20}}
        by_day = {"2099-01-06": prev, "2099-01-05": us_doc}
        with patch.object(storage_mod, "list_openclose_dates",
                          return_value=["2099-01-05", "2099-01-06"]), \
                patch.object(storage_mod, "load_openclose",
                             side_effect=lambda day=None: by_day.get(day, {})):
            rep, label = dm._latest_recorded_before("2099-01-07")
            self.assertEqual(label, "2099-01-06")
            close_lines = dm.build_close_lines(rep, f"{label} (last close)")
            self.assertIn("(last close)", "\n".join(close_lines))
            us_lines = dm.build_us_lines()
            self.assertIn("U.S. market", "\n".join(us_lines))

    def test_open_rebuilds_when_recorded_lacks_india(self):
        """A US-only doc recorded today must NOT pass as the opening mail -
        India is live-built and merged alongside instead."""
        from corporate_actions.core.dates import today_ist
        from corporate_actions.email import daily as dm

        stamped = f"{today_ist().isoformat()}T10:00:00+05:30"
        live = _live_report()
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose",
                             return_value=self._us_only_doc(stamped)), \
                patch("corporate_actions.opening_report.report.collect",
                      return_value=live) as collect, \
                patch("corporate_actions.opening_report.report.save_openclose_doc") as save:
            self.assertTrue(maybe_send_open_email("123"))
        collect.assert_called_once_with(("in",))
        saved_report = save.call_args[0][0]
        self.assertEqual([s["market"] for s in saved_report["sections"]], ["in", "us"])
        self.assertEqual(saved_report["total_target"], 40)
        self.assertIn("opening", self.send.call_args[0][1])

    def test_open_skipped_when_no_india_section(self):
        """US-only report (recorded or live) is never mailed as 'opening'."""
        from corporate_actions.core.dates import today_ist

        stamped = f"{today_ist().isoformat()}T10:00:00+05:30"
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose",
                             return_value=self._us_only_doc(stamped)), \
                patch("corporate_actions.opening_report.report.collect",
                      return_value=self._us_only_doc(stamped)["report"]):
            self.assertFalse(maybe_send_open_email("123"))
        self.send.assert_not_called()

    def test_merge_reports_replaces_stale_india(self):
        from corporate_actions.email.daily import _merge_reports

        base = self._us_only_doc("2026-10-07T10:00:00+05:30")
        base["report"]["sections"].append({
            "market": "in", "universes": [{"key": "in100", "verified": 0, "target": 20}],
            "volume_computed": 0,
        })
        base["report"]["total_verified"] = 0
        base["report"]["total_target"] = 40
        merged = _merge_reports(base["report"], _live_report()["sections"][0])
        self.assertEqual([s["market"] for s in merged["sections"]], ["in", "us"])
        self.assertEqual((merged["total_verified"], merged["total_target"]), (10, 40))

    def test_open_reuses_current_recorded_without_rebuild(self):
        from corporate_actions.core.dates import today_ist

        # Fixed mid-morning IST stamp: immune to UTC/IST midnight edges.
        stamped = f"{today_ist().isoformat()}T10:00:00+05:30"
        current = {"recorded_at": stamped, "report": _live_report()}
        with patch("corporate_actions.market.hours.is_market_open", return_value=True), \
                patch.object(storage_mod, "load_openclose", return_value=current), \
                patch("corporate_actions.opening_report.report.collect",
                      side_effect=AssertionError("must not refetch")):
            self.assertTrue(maybe_send_open_email("123"))
        self.send.assert_called_once()

    def test_eod_skipped_when_no_session_today(self):
        yesterday = date.today() - timedelta(days=1)
        with patch("corporate_actions.opening_report.data.latest_session_date",
                   return_value=yesterday):
            self.assertFalse(maybe_send_eod_email("123"))
        self.send.assert_not_called()

    def test_cron_host_never_mails(self):
        with patch("corporate_actions.config.PROCESS_COMMANDS", False):
            self.assertFalse(maybe_send_open_email("123"))
            self.assertFalse(maybe_send_eod_email("123"))
        self.send.assert_not_called()

    def test_eod_sends_when_traded_today(self):
        from corporate_actions.core.dates import today_ist

        stamped = f"{today_ist().isoformat()}T16:00:00+05:30"
        current = {"recorded_at": stamped, "report": _live_report()}
        with patch("corporate_actions.opening_report.data.latest_session_date",
                   return_value=date.today()), \
                patch.object(storage_mod, "load_openclose", return_value=current), \
                patch.object(storage_mod, "load_watchlist", return_value=[]):
            self.assertTrue(maybe_send_eod_email("123"))
        self.send.assert_called_once()


class WatchlistActionsMailTests(unittest.TestCase):
    def _actions(self):
        from datetime import date, timedelta

        future = (date.today() + timedelta(days=3)).isoformat()
        past = (date.today() - timedelta(days=2)).isoformat()
        return [
            {"symbol": "DIV", "subject": "Dividend Rs 10", "exchange": "NSE",
             "ex_date": future},
            {"symbol": "BONUS", "subject": "Bonus 1:1", "exchange": "NSE",
             "ex_date": past},
            {"symbol": "RIGHTS", "subject": "Rights issue", "exchange": "NSE",
             "ex_date": ""},
        ]

    def test_all_watchlist_actions_grouped_in_mail(self):
        from corporate_actions.email.daily import build_watchlist_actions_lines

        text = "\n".join(build_watchlist_actions_lines(self._actions()))
        for symbol in ("DIV", "BONUS", "RIGHTS"):
            self.assertIn(symbol, text)
        self.assertIn("Upcoming", text)
        self.assertIn("Announced", text)

    def test_eod_prefers_live_actions_over_snapshot(self):
        from corporate_actions.email import daily as dm

        with patch("corporate_actions.email.daily.storage") as store, \
                patch("corporate_actions.poller.fetch_matching",
                      return_value=self._actions()) as fetch:
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = [
                {"symbol": "DIV", "company": "D", "exchange": "NSE"}]
            store.load_schedule_for.return_value = []
            store.load_snapshots.return_value = {
                "corporate_actions": [{"symbol": "OLD", "action": "X"}]}
            store.list_snapshot_dates.return_value = []
            store.list_openclose_dates.return_value = []
            text = "\n".join(dm.build_eod_store_lines("123", []))
        fetch.assert_called_once()
        for symbol in ("DIV", "BONUS", "RIGHTS"):
            self.assertIn(symbol, text)
        self.assertNotIn("OLD", text)

    def test_eod_falls_back_to_snapshot_when_fetch_empty(self):
        from corporate_actions.email import daily as dm

        with patch("corporate_actions.email.daily.storage") as store, \
                patch("corporate_actions.poller.fetch_matching",
                      return_value=[]):
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = []
            store.load_schedule_for.return_value = []
            store.load_snapshots.return_value = {
                "corporate_actions": [{"symbol": "OLD", "action": "X"}]}
            store.list_snapshot_dates.return_value = []
            store.list_openclose_dates.return_value = []
            text = "\n".join(dm.build_eod_store_lines("123", []))
        self.assertIn("OLD", text)


class NiftyActionsMailTests(unittest.TestCase):
    def _feed(self):
        from datetime import date, timedelta

        future = (date.today() + timedelta(days=3)).isoformat()
        past = (date.today() - timedelta(days=2)).isoformat()
        old = (date.today() - timedelta(days=60)).isoformat()
        return [
            {"symbol": "DIV", "subject": "Dividend Rs 10", "exchange": "NSE",
             "ex_date": future},
            {"symbol": "DIV", "subject": "Dividend Rs 10", "exchange": "BSE",
             "ex_date": future},  # NSE/BSE double - dedupes to one
            {"symbol": "BONUS", "subject": "Bonus 1:1", "exchange": "NSE",
             "ex_date": past},
            {"symbol": "SMALL", "subject": "Dividend Rs 1", "exchange": "NSE",
             "ex_date": future},  # not Nifty - excluded
            {"symbol": "OLD", "subject": "Dividend Rs 2", "exchange": "NSE",
             "ex_date": old},  # long completed - excluded
        ]

    def test_fetch_filters_universe_dedupes_and_keeps_inprogress(self):
        from corporate_actions.email import daily as dm

        with patch("corporate_actions.sources.universe.get_index_universe",
                   return_value=["DIV", "BONUS", "OLD"]), \
                patch("corporate_actions.poller.fetch_all_actions",
                      return_value=(self._feed(), [], [])):
            rows = dm.fetch_nifty_actions()
        symbols = [row["symbol"] for row in rows]
        self.assertIn("DIV", symbols)
        self.assertIn("BONUS", symbols)
        self.assertEqual(symbols.count("DIV"), 1)
        self.assertNotIn("SMALL", symbols)
        self.assertNotIn("OLD", symbols)

    def test_nifty_tables_list_everything(self):
        from corporate_actions.email.daily import build_nifty_actions_lines

        text = "\n".join(build_nifty_actions_lines(self._feed()[:3]))
        for symbol in ("DIV", "BONUS"):
            self.assertIn(symbol, text)
        self.assertIn("Nifty 500", text)

    def test_eod_contains_nifty_block(self):
        from corporate_actions.email import daily as dm

        with patch("corporate_actions.email.daily.storage") as store, \
                patch("corporate_actions.poller.fetch_matching", return_value=[]), \
                patch.object(dm, "fetch_nifty_actions",
                             return_value=self._feed()[:1]):
            store.get_user_settings.return_value = {"email": "a@b.com", "daily_email": True}
            store.load_watchlist.return_value = []
            store.load_schedule_for.return_value = []
            store.load_snapshots.return_value = {}
            store.list_snapshot_dates.return_value = []
            store.list_openclose_dates.return_value = []
            text = "\n".join(dm.build_eod_store_lines("123", []))
        self.assertIn("Nifty 500", text)
        self.assertIn("DIV", text)


class ManualMailTests(unittest.TestCase):
    def setUp(self):
        from corporate_actions.bot import email_commands as ec

        self.ec = ec
        self.sent = []
        patches = [
            patch.object(ec, "reply", lambda chat, msg, **kw: self.sent.append(msg)),
            patch.object(ec, "reply_messages",
                         lambda chat, msgs, **kw: self.sent.extend(msgs)),
            patch.object(ec, "send_email", return_value=(True, "")),
            patch.object(ec, "email_configured", return_value=True),
            patch("corporate_actions.poller.fetch_matching", return_value=[]),
            patch("corporate_actions.email.daily.fetch_nifty_actions",
                  return_value=[]),
            patch("corporate_actions.email.daily.fetch_watchlist_quotes",
                  return_value=[]),
            patch.object(storage_mod, "get_user_settings",
                         return_value={"email": "a@b.com", "daily_email": True}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_emailboth_falls_back_to_snapshot_when_nothing_recorded(self):
        snapshot = {"session": "2026-10-08",
                    "gap_downs": [{"symbol": "X", "gap_pct": -2.0, "price": 10.0}]}
        with patch.object(storage_mod, "load_openclose", return_value={}), \
                patch.object(storage_mod, "load_snapshots", return_value=snapshot), \
                patch.object(storage_mod, "load_watchlist", return_value=[]), \
                patch.object(storage_mod, "load_schedule_for", return_value=[]), \
                patch.object(storage_mod, "list_snapshot_dates", return_value=[]), \
                patch.object(storage_mod, "list_openclose_dates", return_value=[]):
            self.ec.handle_emailboth("123", ["/emailboth"])
        self.assertTrue(any("mailed" in str(m).lower() for m in self.sent))

    def test_emailstatus_hides_foreign_and_unattributed_entries(self):
        entries = [
            {"at": "2026-10-08T10:00:00", "chat": "123", "kind": "both",
             "to": "a@b.com", "subject": "mine", "ok": True, "info": ""},
            {"at": "2026-10-08T10:01:00", "chat": "999", "kind": "both",
             "to": "other@x.com", "subject": "theirs", "ok": False, "info": "boom"},
            {"at": "2026-10-08T10:02:00", "chat": "-", "kind": "test",
             "to": "anon@y.com", "subject": "anon", "ok": False, "info": "x"},
        ]
        with patch.object(storage_mod, "load_mail_log", return_value=entries):
            self.ec.handle_emailstatus("123", ["/emailstatus"])
        text = " ".join(str(m) for m in self.sent)
        self.assertIn("mine", text)
        self.assertNotIn("other@x.com", text)
        self.assertNotIn("anon@y.com", text)

    def test_emailstatus_owner_sees_everything(self):
        entries = [
            {"at": "2026-10-08T10:01:00", "chat": "999", "kind": "both",
             "to": "other@x.com", "subject": "theirs", "ok": False, "info": "boom"},
        ]
        with patch.object(storage_mod, "load_mail_log", return_value=entries), \
                patch("corporate_actions.config.TELEGRAM_CHAT_ID", "owner1"):
            self.ec.handle_emailstatus("owner1", ["/emailstatus"])
        self.assertIn("other@x.com", " ".join(str(m) for m in self.sent))


if __name__ == "__main__":
    unittest.main()
