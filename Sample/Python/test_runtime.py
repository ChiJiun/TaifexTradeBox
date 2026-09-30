import json
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from strategy_core import PositionLedger, Quote, StrategyConfig
from ttb.engine import BotEngine, DailyCsvWriter
from ttb.orders import ShadowGateway
from ttb.runtime import RiskStateStore, SessionSchedule


TAIPEI = timezone(timedelta(hours=8))


class SessionScheduleTests(unittest.TestCase):
    def test_regular_session(self):
        schedule = SessionSchedule()
        status = schedule.status(datetime(2026, 9, 29, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(status.phase, "regular")
        self.assertEqual(status.trading_day, "2026-09-29")
        self.assertTrue(status.trading)

    def test_after_hours_groups_with_next_regular_business_day(self):
        schedule = SessionSchedule()
        evening = schedule.status(datetime(2026, 9, 28, 15, 30, tzinfo=TAIPEI))
        early_morning = schedule.status(datetime(2026, 9, 29, 2, 0, tzinfo=TAIPEI))
        self.assertEqual(evening.phase, "after_hours")
        self.assertEqual(evening.trading_day, "2026-09-29")
        self.assertEqual(early_morning.trading_day, "2026-09-29")

    def test_friday_after_hours_rolls_to_monday(self):
        schedule = SessionSchedule()
        status = schedule.status(datetime(2026, 10, 2, 15, 30, tzinfo=TAIPEI))
        self.assertEqual(status.trading_day, "2026-10-05")

    def test_after_hours_can_be_disabled(self):
        schedule = SessionSchedule(include_after_hours=False)
        status = schedule.status(datetime(2026, 9, 28, 16, 0, tzinfo=TAIPEI))
        self.assertEqual(status.phase, "closed")
        self.assertFalse(status.trading)


class DailyCsvWriterTests(unittest.TestCase):
    def test_rotates_at_taipei_calendar_day_boundary(self):
        first = datetime(2026, 9, 30, 23, 59, tzinfo=TAIPEI)
        second = datetime(2026, 10, 1, 0, 1, tzinfo=TAIPEI)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(DailyCsvWriter, "_now", side_effect=[first, second]):
                writer = DailyCsvWriter(Path(directory), "quotes", ["value"])
                first_path = writer.path
                writer.write({"value": 1})
                second_path = writer.path
                writer.close()
            self.assertNotEqual(first_path, second_path)
            self.assertTrue(first_path.exists())
            self.assertTrue(second_path.exists())


class EngineRuntimeTests(unittest.TestCase):
    def test_prolonged_stale_market_data_stops_engine_fail_closed(self):
        class AlwaysTradingSchedule:
            def status(self, _local=None):
                return SimpleNamespace(
                    phase="regular",
                    trading_day="2026-09-30",
                    trading=True,
                    minutes_since_open=60.0,
                    minutes_until_close=60.0,
                )

        config = StrategyConfig(
            symbol="TMFJ6",
            max_order_qty=1,
            max_inventory=1,
            warmup_ticks=2,
        )
        ledger = PositionLedger(config)
        gateway = ShadowGateway(ledger)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runs"
            engine = BotEngine(
                config,
                gateway,
                output,
                session_schedule=AlwaysTradingSchedule(),
                quote_stale_seconds=2.0,
                stale_fatal_seconds=0.05,
            )
            # Tighten only the test instance so the production lower bound can
            # remain conservative.
            engine.quote_stale_seconds = 0.02
            now = time.monotonic()
            engine.start()
            engine.submit(
                Quote(
                    symbol="TMFJ6",
                    bid=20_000,
                    ask=20_001,
                    bid_qty=10,
                    ask_qty=10,
                    last=20_000,
                    last_qty=1,
                    total_volume=1,
                    exchange_time="09:00:00.000",
                    received_at=now,
                )
            )
            deadline = time.monotonic() + 2.0
            while not engine.stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            try:
                self.assertTrue(engine.stop_event.is_set())
                self.assertIn("market data stale", engine.fatal_error)
            finally:
                engine.close()


class RiskStateStoreTests(unittest.TestCase):
    def test_round_trip_only_for_matching_symbol_and_trading_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-state.json"
            store = RiskStateStore(path, "TMFJ6")
            store.checkpoint(
                trading_day="2026-09-30",
                realized_pnl=-1234.5,
                peak_realized_pnl=100.0,
                fees_and_tax=55.0,
                fill_count=7,
                peak_equity=250.0,
                halt_reason="session loss limit",
                position=0,
            )
            restored = store.load("2026-09-30")
            self.assertEqual(restored["realized_pnl"], -1234.5)
            self.assertEqual(restored["fill_count"], 7)
            self.assertEqual(restored["halt_reason"], "session loss limit")
            self.assertEqual(store.load("2026-10-01"), {})
            self.assertEqual(RiskStateStore(path, "TXFJ6").load("2026-09-30"), {})

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 1)
            self.assertEqual(raw["symbol"], "TMFJ6")


if __name__ == "__main__":
    unittest.main()
