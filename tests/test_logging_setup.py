"""Tests for corporate_actions.logging_setup (centralized logging)."""
import logging
import logging.handlers
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from corporate_actions import logging_setup

_OWN_HANDLERS = (
    logging_setup.ImmediateStreamHandler,
    logging.handlers.RotatingFileHandler,
)


class SetupLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)
        root = logging.getLogger()
        # Snapshot the ambient handler set (app imports via other test modules
        # may have configured root already) and strip our handler types so
        # every test starts from a clean root.
        self._saved_handlers = root.handlers[:]
        self._saved_level = root.level
        self.addCleanup(self._restore)
        for handler in self._saved_handlers:
            if isinstance(handler, _OWN_HANDLERS):
                root.removeHandler(handler)  # not closed; restored afterwards
        root.setLevel(logging.DEBUG)
        logging_setup._configured = False

    def _restore(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if handler not in self._saved_handlers:
                handler.close()
                root.removeHandler(handler)
        for handler in self._saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(self._saved_level)
        logging_setup._configured = True

    def _setup(self, *, level=None, log_dir=None):
        return logging_setup.setup_logging(
            level=level,
            log_dir=log_dir if log_dir is not None else self.log_dir,
        )

    def test_creates_rotating_application_and_error_logs(self):
        self._setup()
        logging.getLogger("probe").info("info line")
        logging.getLogger("probe").error("error line")
        for handler in logging.getLogger().handlers:
            handler.flush()
        app_log = self.log_dir / "application.log"
        err_log = self.log_dir / "error.log"
        self.assertTrue(app_log.exists())
        self.assertTrue(err_log.exists())
        self.assertIn("info line", app_log.read_text(encoding="utf-8"))
        self.assertIn("error line", err_log.read_text(encoding="utf-8"))
        self.assertNotIn("info line", err_log.read_text(encoding="utf-8"))

    def test_file_handlers_are_rotating(self):
        self._setup()
        rotating = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        self.assertEqual(len(rotating), 2)

    def test_log_level_env_controls_console(self):
        self._setup(level="WARNING")
        root = logging.getLogger()
        console = [
            handler
            for handler in root.handlers
            if isinstance(handler, logging_setup.ImmediateStreamHandler)
        ]
        self.assertEqual(len(console), 1)
        self.assertEqual(console[0].level, logging.WARNING)

    def test_unwritable_log_dir_does_not_crash_startup(self):
        blocker = self.log_dir / "occupied"
        blocker.write_text("x", encoding="utf-8")
        with self.assertLogs("corporate_actions.logging_setup", level="WARNING"):
            self._setup(log_dir=blocker)  # must not raise

    def test_repeat_call_does_not_duplicate_handlers(self):
        self._setup()
        count = len(logging.getLogger().handlers)
        # A natural repeat (flag stays True) must keep the existing handlers.
        self.assertTrue(logging_setup._configured)
        logging_setup.setup_logging(log_dir=self.log_dir)
        self.assertEqual(len(logging.getLogger().handlers), count)


if __name__ == "__main__":
    unittest.main()
