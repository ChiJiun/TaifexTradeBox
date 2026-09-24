import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    def test_session_phase_only_marks_day_session_as_trading(self):
        weekday = datetime(2026, 9, 23, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        closed = datetime(2026, 9, 23, 14, 0, tzinfo=timezone(timedelta(hours=8)))
        weekend = datetime(2026, 9, 26, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(BotEngine._session_phase(weekday), "trading")
        self.assertEqual(BotEngine._session_phase(closed), "closed")
        self.assertEqual(BotEngine._session_phase(weekend), "closed")


if __name__ == "__main__":
    unittest.main()
