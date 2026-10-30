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

    def test_trading_session_without_any_first_quote_stops_engine(self):
        class AlwaysTradingSchedule:
            def status(self, _local=None):
                return SimpleNamespace(
                    phase="regular", trading_day="2026-09-30", trading=True,
                    minutes_since_open=60.0, minutes_until_close=60.0,
                )

        config = StrategyConfig(symbol="TMFJ6", max_order_qty=1, max_inventory=1, warmup_ticks=2)
        gateway = ShadowGateway(PositionLedger(config))
        with tempfile.TemporaryDirectory() as directory:
            engine = BotEngine(
                config, gateway, Path(directory) / "runs",
                session_schedule=AlwaysTradingSchedule(), stale_fatal_seconds=0.05,
            )
            engine.first_quote_grace_seconds = 0.02
            engine.start()
            deadline = time.monotonic() + 3.0
            while not engine.stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            try:
                self.assertTrue(engine.stop_event.is_set())
                self.assertIn("market data stale", engine.fatal_error)
            finally:
                engine.close()


    def test_quiet_gap_between_ticks_keeps_the_trading_state(self):
        # 2026-10-30: the CCF grid held 1 lot between sparse ticks, and every
        # idle half second rewrote health to manual_reconciliation_required
        # (or waiting_market when flat), so the watchdog paged exit 3 and 2.
        class AlwaysTradingSchedule:
            def status(self, _local=None):
                return SimpleNamespace(phase="regular", trading_day="2026-10-30", trading=True,
                                       minutes_since_open=60.0, minutes_until_close=60.0, reason="")

        config = StrategyConfig(symbol="CCFK6", max_order_qty=1, max_inventory=1, warmup_ticks=2)
        ledger = PositionLedger(config)
        with tempfile.TemporaryDirectory() as directory:
            engine = BotEngine(
                config, ShadowGateway(ledger), Path(directory) / "runs",
                session_schedule=AlwaysTradingSchedule(), quote_stale_seconds=300, stale_fatal_seconds=900,
            )
            ledger.position = 1
            engine.last_quote = Quote(
                symbol="CCFK6", bid=108.0, ask=108.5, bid_qty=5, ask_qty=5, last=108.0, last_qty=1,
                total_volume=1, exchange_time="10:30:00", received_at=time.monotonic(),
            )
            engine.start()
            engine.health.update(state="healthy")  # as the last processed tick leaves it
            try:
                time.sleep(1.5)
                snapshot = engine.health.snapshot()
                self.assertEqual(snapshot["state"], "healthy")
                self.assertEqual(snapshot["position"], 1)
            finally:
                engine.close()

class RiskStateStoreTests(unittest.TestCase):
    def test_locked_risk_state_is_retried_then_fails_closed(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-state.json"
            store = RiskStateStore(path, "TMFK6")
            values = dict(trading_day="2026-10-29", realized_pnl=0.0, peak_realized_pnl=0.0,
                          fees_and_tax=0.0, fill_count=0, peak_equity=0.0, halt_reason="", position=0)
            real_replace, calls = os.replace, {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "Access is denied")
                real_replace(src, dst)

            with mock.patch("ttb.runtime.os.replace", side_effect=flaky), mock.patch("ttb.runtime.time.sleep"):
                store.checkpoint(**values)
            self.assertEqual(store.load("2026-10-29")["fill_count"], 0)
            locked = PermissionError(5, "Access is denied")
            with mock.patch("ttb.runtime.os.replace", side_effect=locked), mock.patch("ttb.runtime.time.sleep"):
                with self.assertRaises(PermissionError):
                    store.checkpoint(**{**values, "fill_count": 1})

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
            with self.assertRaisesRegex(RuntimeError, "symbol mismatched"):
                RiskStateStore(path, "TXFJ6").load("2026-09-30")

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 1)
            self.assertEqual(raw["symbol"], "TMFJ6")


if __name__ == "__main__":
    unittest.main()
