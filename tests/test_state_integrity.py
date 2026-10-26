"""State integrity regressions. Git tests use disposable local repositories only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from corporate_actions import config, github
from corporate_actions.storage import seen, settings, users, watchlist
from corporate_actions.storage.json_file import InvalidStateError, _file_lock, read_json, state_lock, write_json
from corporate_actions.storage.migrate import migrate_legacy_state_files


class StorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for attr, value in {
            "BASE_DIR": self.root,
            "SEEN_FILE": self.root / "seen.json",
            "SETTINGS_FILE": self.root / "settings.json",
            "WATCHLIST_FILE": self.root / "watchlist.json",
            "SUBSCRIPTIONS_FILE": self.root / "subscriptions.json",
            "TELEGRAM_CHAT_ID": "owner",
        }.items():
            p = patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_strict_root_schema_and_quarantine(self):
        path = self.root / "state.json"
        for raw, default in [('[]', {}), ('{}', []), ('null', {}), ('true', []), ('{broken', {})]:
            with self.subTest(raw=raw, default=default):
                path.write_text(raw, encoding="utf-8")
                before = set(self.root.glob("*.bak"))
                with self.assertRaises(InvalidStateError):
                    read_json(path, default)
                backups = set(self.root.glob("*.bak")) - before
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups.pop().read_text(encoding="utf-8"), raw)
                self.assertEqual(path.read_text(encoding="utf-8"), raw)
                with self.assertRaises(InvalidStateError):
                    write_json(path, default)
                self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_direct_write_preserves_corrupt_original(self):
        path = self.root / "state.json"
        path.write_bytes(b'\xff{broken')
        with self.assertRaises(InvalidStateError):
            write_json(path, {"ok": True})
        self.assertEqual(next(self.root.glob("*.bak")).read_bytes(), b'\xff{broken')
        self.assertEqual(path.read_bytes(), b'\xff{broken')

    def test_failed_quarantine_does_not_overwrite(self):
        path = self.root / "state.json"
        path.write_text("{broken", encoding="utf-8")
        with patch("corporate_actions.storage.json_file._quarantine", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                write_json(path, {})
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_read_io_error_is_not_empty_state(self):
        with patch.object(Path, "open", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                read_json(self.root / "state.json", {})

    def test_missing_default_is_not_shared(self):
        default = {"nested": []}
        result = read_json(self.root / "missing.json", default)
        result["nested"].append(1)
        self.assertEqual(default, {"nested": []})

    def test_stale_seen_saves_union_disk_keys(self):
        seen.save_seen({"first"})
        stale = seen.load_seen()
        seen.save_seen({"other-writer"})
        seen.save_seen(stale | {"last"})
        self.assertEqual(seen.load_seen(), {"first", "other-writer", "last"})

    def test_settings_patch_preserves_recent_and_mail_markers(self):
        settings.patch_user_settings("chat", {"email": "old", "quiet_until": 5})
        settings.record_recent_command("chat", "/stock ITC")
        settings.patch_user_settings("chat", {"last_daily_email": "today"})
        result = settings.patch_user_settings("chat", {"email": "new"}, remove=("quiet_until",))
        self.assertEqual(result, {"email": "new", "recent_commands": ["/stock ITC"],
                                  "last_daily_email": "today"})
        result["recent_commands"].clear()
        self.assertEqual(settings.get_recent_commands("chat"), ["/stock ITC"])

    def test_settings_update_exception_is_not_saved(self):
        settings.patch_user_settings("chat", {"value": 1})

        def broken(current):
            current["value"] = 2
            raise RuntimeError("cancel")

        with self.assertRaises(RuntimeError):
            settings.update_user_settings("chat", broken)
        self.assertEqual(settings.get_user_settings("chat"), {"value": 1})

    def test_thread_updates_are_atomic(self):
        errors = []

        def worker():
            try:
                for _ in range(15):
                    settings.update_user_settings("chat", lambda s: s.update(count=s.get("count", 0) + 1))
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=worker) for _ in range(3)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(10)
            self.assertFalse(worker_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(settings.get_user_settings("chat")["count"], 45)

    def test_cross_process_updates_are_atomic(self):
        script = (
            "import sys; from pathlib import Path; from corporate_actions import config; "
            "from corporate_actions.storage.settings import update_user_settings; "
            "config.BASE_DIR=Path(sys.argv[1]); config.SETTINGS_FILE=config.BASE_DIR/'settings.json'; "
            "[update_user_settings('chat', lambda s: s.update(count=s.get('count',0)+1)) for _ in range(15)]"
        )
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.root)],
                                      cwd=Path(__file__).resolve().parents[1],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        try:
            for process in processes:
                _, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(settings.get_user_settings("chat")["count"], 45)

    def test_nested_lock_and_direct_write_are_reentrant(self):
        path = self.root / "state.json"
        with state_lock(), _file_lock(path), _file_lock(path):
            write_json(path, {"saved": True})
            self.assertEqual(read_json(path, {}), {"saved": True})

    def test_owner_and_subscriber_batch_summary_and_exchange_dedup(self):
        items = [{"symbol": "ITC", "exchange": "NSE"},
                 {"symbol": "ITC", "exchange": "BSE"},
                 {"symbol": "itc", "exchange": "nse"}]
        for chat in ("owner", "subscriber"):
            result = users.bulk_add_to_user_list(chat, items)
            self.assertEqual(result["added"], 2)
            self.assertEqual(result["skipped_duplicates"], 1)
            self.assertEqual(len(result["list"]), 2)
            result = users.bulk_add_to_user_list(chat, items)
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["skipped_duplicates"], 3)
        self.assertIsInstance(watchlist.add_to_watchlist(items), list)

    def test_migration_never_overwrites_existing_destination(self):
        destination = self.root / "data" / "watchlist.json"
        destination.parent.mkdir()
        destination.write_text('[{"symbol":"NEW"}]', encoding="utf-8")
        legacy = self.root / "watchlist.json"
        legacy.write_text('[{"symbol":"OLD"}]', encoding="utf-8")
        with patch.object(config, "WATCHLIST_FILE", destination):
            self.assertNotIn("watchlist.json", migrate_legacy_state_files(self.root))
        self.assertEqual(read_json(destination, [])[0]["symbol"], "NEW")
        self.assertTrue(legacy.exists())

    def test_relative_environment_paths_resolve_from_base(self):
        with patch.dict(os.environ, {"TEST_STATE_PATH": "custom/state.json"}):
            self.assertEqual(config._env_path("TEST_STATE_PATH", "unused"),
                             self.root / "custom" / "state.json")


class EntrypointIntegrityTests(unittest.TestCase):
    def test_runner_push_failure_is_not_a_successful_cron(self):
        from corporate_actions.bot import runner
        from corporate_actions.poller import engine

        with patch.object(sys, "argv", ["run_bot.py"]), \
             patch.object(runner, "sync_state", return_value=True), \
             patch.object(runner, "register_commands"), \
             patch.object(runner, "process_commands", return_value=None), \
             patch.object(runner.poller, "run_once", return_value=0), \
             patch.object(engine, "_snapshots_auto_default", return_value=False), \
             patch.object(engine, "_snapshots_auto_enabled", False), \
             patch.object(runner, "push_state", return_value=False), \
             patch.object(runner, "last_push_error", return_value="rejected"):
            with self.assertRaisesRegex(RuntimeError, "State persistence failed: rejected"):
                runner.main()

    def test_failed_initial_sync_stops_runner_before_commands(self):
        from corporate_actions.bot import runner

        with patch.object(sys, "argv", ["run_bot.py"]), \
             patch.object(runner, "sync_state", return_value=False), \
             patch.object(runner, "last_push_error", return_value="fetch failed"), \
             patch.object(runner, "register_commands") as register:
            with self.assertRaisesRegex(RuntimeError, "Initial state sync failed"):
                runner.main()
            register.assert_not_called()

    def test_bot_sync_and_flush_precede_background_writers(self):
        with patch("corporate_actions.logging_setup.setup_logging"):
            import bot_server

        events = []
        with patch.dict(os.environ, {"SERVE_DASHBOARD": "false"}), \
             patch.object(bot_server, "sync_state", side_effect=lambda: events.append("sync") or True), \
             patch.object(bot_server, "flush_pending_state", side_effect=lambda: events.append("flush")), \
             patch.object(bot_server, "register_commands", side_effect=lambda: events.append("register")), \
             patch.object(bot_server, "start_scheduled_reports", side_effect=RuntimeError("stop-before-threads")), \
             patch.object(bot_server, "_deployed_commit", return_value="fixture"):
            with self.assertRaisesRegex(RuntimeError, "stop-before-threads"):
                bot_server.main()
        self.assertEqual(events, ["sync", "flush", "register"])


class GitInvocationTests(unittest.TestCase):
    def test_git_http_version_cwd_and_noninteractive_environment(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(github.subprocess, "run", return_value=result) as run, \
             patch.dict(os.environ, {"GIT_HTTP_VERSION": "HTTP/1.1"}):
            github._git("git", "fetch", "origin")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ["git", "--no-pager", "-c", "http.version=HTTP/1.1"])
        self.assertEqual(args[0][4:], ["fetch", "origin"])
        self.assertEqual(kwargs["cwd"], config.BASE_DIR)
        self.assertEqual(kwargs["env"]["GIT_EDITOR"], "true")
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")


    def test_git_accepts_bare_subcommand_and_honors_transport_override(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        for version in ("HTTP/2", ""):
            with self.subTest(version=version), \
                 patch.object(github.subprocess, "run", return_value=result) as run, \
                 patch.dict(os.environ, {"GIT_HTTP_VERSION": version}):
                github._git("push", "origin")
                command = run.call_args.args[0]
                self.assertEqual(command[0], "git")
                self.assertEqual(command[-2:], ["push", "origin"])
                self.assertEqual("http.version=" + version in command, bool(version))
        with patch.object(github.subprocess, "run", return_value=result) as run:
            github._git("git", "status", "--porcelain")
            self.assertEqual(run.call_args.args[0], ["git", "--no-pager", "status", "--porcelain"])


class GitStateIntegrationTests(unittest.TestCase):
    """Exercise real Git rejection/rebase/fast-forward behavior without a network."""
    def git(self, cwd, *args):
        result = subprocess.run(["git", "--no-pager", *args], cwd=cwd,
                                env=dict(os.environ, GIT_EDITOR="true", GIT_TERMINAL_PROMPT="0"),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.local, self.peer, self.remote = root / "local", root / "peer", root / "remote.git"
        self.local.mkdir()
        self.git(root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.local, "init", "--initial-branch=main")
        self.git(self.local, "config", "user.name", "Test")
        self.git(self.local, "config", "user.email", "test@example.invalid")
        self.git(self.local, "config", "core.autocrlf", "false")
        (self.local / ".gitignore").write_text("*.lock\n*.bak\n*.tmp\n", encoding="utf-8")
        (self.local / "data").mkdir()
        self.path = self.local / "data" / "settings.json"
        self.path.write_text('{"base": 1}', encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Seed fixture")
        self.git(self.local, "remote", "add", "origin", str(self.remote))
        self.git(self.local, "push", "-u", "origin", "main")
        self.git(root, "clone", str(self.remote), str(self.peer))
        self.git(self.peer, "config", "user.name", "Test")
        self.git(self.peer, "config", "user.email", "test@example.invalid")
        for p in (patch.object(config, "BASE_DIR", self.local),
                  patch.object(github, "STATE_FILES", (self.path, self.local / "data" / "seen.json")),
                  patch.dict(os.environ, {"GH_TOKEN": "fixture-token", "GITHUB_REPOSITORY": "fixture/repo",
                                          "GH_PUSH_BRANCH": "main"})):
            p.start()
            self.addCleanup(p.stop)
        self.real_git = github._git
        self.calls = []

        def local_git(*args, **kwargs):
            self.calls.append(args)
            args = tuple(str(self.remote) if arg.startswith("https://x-access-token:") else arg for arg in args)
            return self.real_git(*args, **kwargs)

        p = patch.object(github, "_git", side_effect=local_git)
        self.mock_git = p.start()
        self.addCleanup(p.stop)

    def peer_commit(self, relative, content):
        path = self.peer / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git(self.peer, "add", relative)
        self.git(self.peer, "commit", "-m", "Remote writer")
        self.git(self.peer, "push", "origin", "main")

    def test_untracked_state_detected_and_pushed_without_lock_files(self):
        fresh = self.local / "data" / "seen.json"
        write_json(fresh, ["new"])
        self.assertIn("data/seen.json", github.pending_state_changes())
        self.assertTrue(github.push_state(), github.last_push_error())
        self.assertEqual(self.git(self.remote, "show", "main:data/seen.json"), '[\n  "new"\n]')
        self.assertNotIn(".lock", self.git(self.local, "ls-files"))

    def test_tracked_deleted_state_detected_and_pushed(self):
        self.path.unlink()
        self.assertIn("data/settings.json", github.pending_state_changes())
        self.assertTrue(github.push_state(), github.last_push_error())
        self.assertNotIn("data/settings.json", self.git(self.remote, "ls-tree", "-r", "--name-only", "main"))

    def test_ahead_clean_rejected_push_reconciles(self):
        self.path.write_text('{"local": 2}', encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Unpushed local state")
        self.peer_commit("data/seen.json", '["remote-key"]')
        self.assertTrue(github.push_state(), github.last_push_error())
        self.assertEqual(read_json(self.path, {}), {"local": 2})
        self.assertEqual(read_json(self.local / "data" / "seen.json", []), ["remote-key"])
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"),
                         self.git(self.remote, "rev-parse", "main"))

    def test_semantic_conflict_preserves_original_local_commit_and_data(self):
        self.path.write_text('{"base": 2}', encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Unpushed local state")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.peer_commit("data/settings.json", '{"base": 3}')
        self.assertFalse(github.push_state())
        self.assertIn("original local commits preserved", github.last_push_error())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertEqual(read_json(self.path, {}), {"base": 2})
        self.assertEqual(self.git(self.remote, "show", "main:data/settings.json"), '{"base": 3}')
        self.assertEqual(self.git(self.local, "status", "--porcelain"), "")
        self.assertFalse(any("--hard" in args for args in self.calls))

    def test_failed_fetch_stops_rebase_and_sync(self):
        def fail_fetch(*args, **kwargs):
            self.calls.append(args)
            if "push" in args or "fetch" in args:
                return subprocess.CompletedProcess(args, 1, "", "network unavailable")
            return self.real_git(*args, **kwargs)

        self.mock_git.side_effect = fail_fetch
        self.assertFalse(github.push_state())
        self.assertIn("fetch failed", github.last_push_error())
        self.assertFalse(any("rebase" in args for args in self.calls))
        self.calls.clear()
        self.assertFalse(github.sync_state())
        self.assertFalse(any("merge" in args or "reset" in args for args in self.calls))

    def test_new_state_commit_rejected_push_also_reconciles(self):
        self.path.write_text('{"local": 2}', encoding="utf-8")
        self.peer_commit("data/seen.json", '["remote-key"]')
        self.assertTrue(github.push_state(), github.last_push_error())
        self.assertEqual(read_json(self.path, {}), {"local": 2})
        self.assertEqual(read_json(self.local / "data" / "seen.json", []), ["remote-key"])

    def test_status_failure_is_not_reported_as_clean(self):
        def fail_status(*args, **kwargs):
            if "status" in args:
                return subprocess.CompletedProcess(args, 1, "", "index unavailable")
            return self.real_git(*args, **kwargs)

        self.mock_git.side_effect = fail_status
        with self.assertRaisesRegex(RuntimeError, "git status failed"):
            github.pending_state_changes()
        self.assertFalse(github.sync_state())
        self.assertIn("git status failed", github.last_push_error())

    def test_sync_fast_forwards_clean_tree_and_keeps_dirty_state(self):
        self.peer_commit("data/settings.json", '{"remote": 3}')
        self.assertTrue(github.sync_state())
        self.assertEqual(read_json(self.path, {}), {"remote": 3})
        write_json(self.path, {"dirty-local": 4})
        self.peer_commit("data/settings.json", '{"remote": 5}')
        self.assertTrue(github.sync_state())
        self.assertEqual(read_json(self.path, {}), {"dirty-local": 4})
        self.assertFalse(any("--hard" in args for args in self.calls))

    def test_sync_keeps_untracked_state(self):
        fresh = self.local / "data" / "seen.json"
        fresh.write_text('["local"]', encoding="utf-8")
        self.peer_commit("data/seen.json", '["remote"]')
        self.assertTrue(github.sync_state())
        self.assertEqual(read_json(fresh, []), ["local"])

    def test_unrelated_staged_code_is_not_committed(self):
        path = self.local / "code.py"
        path.write_text("# user work\n", encoding="utf-8")
        self.git(self.local, "add", "code.py")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.assertFalse(github.push_state())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertIn("code.py", self.git(self.local, "diff", "--cached", "--name-only"))

    def test_independent_nested_settings_merge_without_text_merge(self):
        self.path.write_text('{"base": 1, "chat": {"email": "local"}}', encoding="utf-8")
        self.peer_commit("data/settings.json", '{"base": 1, "chat": {"quiet": true}}')
        self.assertTrue(github.push_state(), github.last_push_error())
        self.assertEqual(json.loads(self.path.read_text()),
                         {"base": 1, "chat": {"email": "local", "quiet": True}})
        self.assertFalse(any("rebase" in args for args in self.calls))
        self.assertTrue(all("--ff-only" in args for args in self.calls if "merge" in args))

    def test_commit_and_reconciliation_use_bot_identity_without_host_config(self):
        self.git(self.local, "config", "--unset", "user.name")
        self.git(self.local, "config", "--unset", "user.email")
        self.path.write_text('{"base": 1, "local": 2}', encoding="utf-8")
        self.peer_commit("data/seen.json", '["remote"]')
        # An isolated global config proves identity comes from the command,
        # not the developer/CI host or a configured repository identity.
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull,
                                     "GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertTrue(github.push_state(), github.last_push_error())
        self.assertEqual(self.git(self.local, "log", "-1", "--format=%cn <%ce>"),
                         "github-actions <actions@github.com>")
        self.assertEqual(self.git(self.local, "log", "-1", "--format=%cn <%ce>", "HEAD^1"),
                         "github-actions <actions@github.com>")

    def test_operation_markers_block_push_before_staging_and_sync_before_fetch(self):
        for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "sequencer"):
            with self.subTest(marker=marker):
                path = self.local / ".git" / marker
                path.mkdir() if marker in ("rebase-merge", "rebase-apply", "sequencer") else path.touch()
                self.path.write_text('{"pending": true}', encoding="utf-8")
                self.calls.clear()
                self.assertFalse(github.push_state())
                self.assertFalse(github.sync_state())
                self.assertFalse(any("add" in args or "fetch" in args or "push" in args for args in self.calls))
                path.rmdir() if path.is_dir() else path.unlink()

    def test_unmerged_index_blocks_push_and_sync(self):
        oid = self.git(self.local, "rev-parse", "HEAD:data/settings.json")
        self.git(self.local, "update-index", "--force-remove", "data/settings.json")
        # Binary stdin: text mode would rewrite "\n" as "\r\n" on Windows and
        # git would reject the path ("Ignoring path ...") instead of staging
        # the conflict entry, leaving the test with nothing to detect.
        result = subprocess.run(
            ["git", "--no-pager", "update-index", "--add", "--index-info"],
            cwd=self.local,
            input=f"100644 {oid} 1\tdata/settings.json\n".encode("utf-8"),
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.local, "ls-files", "--unmerged"),
                         f"100644 {oid} 1\tdata/settings.json")
        self.calls.clear()
        self.assertFalse(github.push_state())
        self.assertFalse(github.sync_state())
        self.assertFalse(any("add" in args or "fetch" in args for args in self.calls))

    def test_invalid_worktree_json_never_staged_or_published(self):
        before = self.git(self.local, "rev-parse", "HEAD")
        for raw in ('{"chat": {"email": "first", "email": "lost"}}',
                    '{"number": NaN}', '{"number": Infinity}', '{"number": 1e999}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                self.calls.clear()
                self.assertFalse(github.push_state())
                self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
                self.assertEqual(self.path.read_text(), raw)
                self.assertFalse(any("add" in args or "push" in args for args in self.calls))

    def test_invalid_unchanged_json_in_head_prevents_publish(self):
        self.path.write_text('{"same":1,"same":2}', encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Invalid state fixture")
        (self.local / "data" / "seen.json").write_text('["new"]', encoding="utf-8")
        self.calls.clear()
        self.assertFalse(github.push_state())
        self.assertFalse(any("push" in args for args in self.calls))

    def test_invalid_remote_json_blocks_sync_and_reconciliation(self):
        before = self.git(self.local, "rev-parse", "HEAD")
        self.peer_commit("data/seen.json", '[NaN]')
        self.assertFalse(github.sync_state())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.path.write_text('{"local": 2}', encoding="utf-8")
        self.assertFalse(github.push_state())
        local_tip = self.git(self.local, "rev-parse", "HEAD")
        self.assertNotEqual(local_tip, before)
        self.assertEqual(json.loads(self.path.read_text()), {"local": 2})
        self.assertFalse((self.local / "data" / "seen.json").exists())
        self.assertEqual(sum("push" in args for args in self.calls), 1)

    def test_failed_merge_commit_keeps_index_worktree_and_recovery_tip(self):
        self.path.write_text('{"local": 2}', encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Local state")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.peer_commit("data/seen.json", '["remote"]')
        route = self.mock_git.side_effect
        def fail_commit(*args, **kwargs):
            if "commit-tree" in args:
                return subprocess.CompletedProcess(args, 1, "", "injected commit failure")
            return route(*args, **kwargs)
        self.mock_git.side_effect = fail_commit
        self.assertFalse(github.push_state())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.local, "status", "--porcelain"), "")
        self.assertEqual(json.loads(self.path.read_text()), {"local": 2})
        self.assertIn(before, self.git(self.local, "for-each-ref", "--format=%(objectname)",
                                     "refs/state-backups/"))

    def test_local_code_commit_is_not_silently_replayed(self):
        (self.local / "code.py").write_text("local = 1\n", encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Local code")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.peer_commit("data/seen.json", '["remote"]')
        self.assertFalse(github.push_state())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)

    def test_sync_waits_for_storage_transaction(self):
        started, finished = threading.Event(), threading.Event()
        errors = []

        def sync():
            started.set()
            try:
                github.sync_state()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with state_lock():
            thread = threading.Thread(target=sync)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
            write_json(self.path, {"concurrent": True})
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(read_json(self.path, {}), {"concurrent": True})

    def test_schedule_list_root_validated_and_pushed(self):
        """schedule.json is list-rooted; _load_state must not reject it as non-dict."""
        sched = self.local / "data" / "schedule.json"
        content = [{"interval_min": 60, "commands": ["/report"], "chat": "123"}]
        sched.write_text(json.dumps(content), encoding="utf-8")
        with patch.object(github, "STATE_FILES",
                          (self.path, self.local / "data" / "seen.json", sched)):
            self.assertTrue(github.push_state(), github.last_push_error())
        pushed = json.loads(self.git(self.remote, "show", "main:data/schedule.json"))
        self.assertEqual(pushed, content)

    def test_schedule_atomic_conflict_fails_closed_and_preserves_local(self):
        """Diverging schedule lists fail closed; local commit and data are untouched."""
        sched = self.local / "data" / "schedule.json"
        local_content = [{"interval_min": 60, "commands": ["/report"], "chat": "123"}]
        sched.write_text(json.dumps(local_content), encoding="utf-8")
        self.git(self.local, "add", "data/schedule.json")
        self.git(self.local, "commit", "-m", "Local schedule")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.peer_commit("data/schedule.json",
                         json.dumps([{"interval_min": 120, "commands": ["/scan"],
                                      "chat": "456"}]))
        with patch.object(github, "STATE_FILES",
                          (self.path, self.local / "data" / "seen.json", sched)):
            self.assertFalse(github.push_state())
        self.assertIn("original local commits preserved", github.last_push_error())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertEqual(json.loads(sched.read_text()), local_content)
        self.assertFalse(any("--hard" in args for args in self.calls))

    def test_recovery_ref_retained_after_successful_reconciliation(self):
        """Backup ref is written before any merge attempt, even when merge succeeds."""
        self.path.write_text('{"base": 1, "local": 2}', encoding="utf-8")
        self.git(self.local, "add", ".")
        self.git(self.local, "commit", "-m", "Local state")
        before = self.git(self.local, "rev-parse", "HEAD")
        # Peer adds a different file so reconcile succeeds cleanly.
        self.peer_commit("data/seen.json", '["remote"]')
        self.assertTrue(github.push_state(), github.last_push_error())
        refs = self.git(self.local, "for-each-ref",
                        "--format=%(objectname)", "refs/state-backups/")
        self.assertIn(before, refs.splitlines(),
                      "Local tip must be preserved in refs/state-backups/ "
                      "even after a successful reconciliation")


if __name__ == "__main__":
    unittest.main()
