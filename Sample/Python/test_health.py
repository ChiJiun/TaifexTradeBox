import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ttb.engine import BotEngine
from ttb.health import HealthReporter


class HealthReporterTests(unittest.TestCase):
    def test_atomic_health_file_contains_latest_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            reporter = HealthReporter(path, symbol="TMFJ6", mode="shadow")
            reporter.update(state="healthy", position=0)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["symbol"], "TMFJ6")
            self.assertEqual(value["state"], "healthy")
            self.assertEqual(value["position"], 0)
            self.assertTrue(value["updated_at"])

    def test_locked_health_file_is_retried_not_fatal(self):
        # A watchdog reading health.json without FILE_SHARE_DELETE makes
        # os.replace fail with WinError 5; that must never stop the bot.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            reporter = HealthReporter(path, symbol="TMFK6", mode="ttb")
            real_replace = os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(5, "Access is denied")
                real_replace(src, dst)

            with mock.patch("ttb.health.os.replace", side_effect=flaky), mock.patch("ttb.health.time.sleep"):
                reporter.update(state="healthy")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "healthy")

            locked = PermissionError(5, "Access is denied")
            with mock.patch("ttb.health.os.replace", side_effect=locked), mock.patch("ttb.health.time.sleep"):
                with self.assertLogs("ttb.health", "WARNING"):
                    reporter.update(state="paused")
            self.assertEqual(reporter.snapshot()["state"], "paused")
            reporter.update(position=1)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((value["state"], value["position"]), ("paused", 1))

    def test_session_phase_only_marks_day_session_as_trading(self):
        weekday = datetime(2026, 9, 23, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        closed = datetime(2026, 9, 23, 14, 0, tzinfo=timezone(timedelta(hours=8)))
        weekend = datetime(2026, 9, 26, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(BotEngine._session_phase(weekday), "trading")
        self.assertEqual(BotEngine._session_phase(closed), "closed")
        self.assertEqual(BotEngine._session_phase(weekend), "closed")


if __name__ == "__main__":
    unittest.main()
