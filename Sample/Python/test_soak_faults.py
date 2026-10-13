from __future__ import annotations

import concurrent.futures
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from automation.soak_offline import accelerated_soak
from strategy_core import OrderIntent, PositionLedger, Quote, StrategyConfig
from ttb.engine import BotEngine
from ttb.orders import ShadowGateway, TTBOrderGateway
from ttb.risk import PreTradeLimits
from ttb.status_server import StatusServer


def quote():
    return Quote(
        symbol="TXFA7", bid=20000, ask=20001,
        bid_qty=10, ask_qty=10, last=20000,
        last_qty=1, total_volume=1,
        exchange_time="09:00:00",
        received_at=time.monotonic(),
    )


class AcceleratedSoakTests(unittest.TestCase):
    def test_offline_48h_cross_day_rotation_and_bounded_growth(self):
        result = accelerated_soak(hours=48, interval_minutes=10)
        self.assertEqual(result["horizon_hours"], 48)
        self.assertEqual(result["ticks_processed"], 288)
        self.assertEqual(result["quotes_saved"], 288)
        self.assertEqual(result["decisions_saved"], 288)
        self.assertEqual(result["quote_partitions"], 3)
        self.assertEqual(result["decision_partitions"], 3)
        self.assertEqual(result["position"], 0)
        self.assertEqual(result["broker_orders_submitted"], 0)
        self.assertLess(result["total_output_bytes"], 5_000_000)

    def test_worker_disk_failure_cannot_leave_main_loop_waiting_forever(self):
        with tempfile.TemporaryDirectory() as folder:
            config = StrategyConfig(symbol="TXFA7")
            gateway = ShadowGateway(PositionLedger(config))
            engine = BotEngine(config, gateway, Path(folder) / "runs",
                               risk_state_path=Path(folder) / "risk.json")
            engine.start()
            previous_health_update = engine.health.update
            with patch.object(engine.raw_recorder, "write", side_effect=OSError("disk full")), \
                 patch.object(engine.health, "update", side_effect=OSError("disk full")):
                engine.submit(quote())
                self.assertTrue(engine.stop_event.wait(3.0))
                self.assertIn("disk full", engine.fatal_error)
            # Restoring the health writer lets local cleanup complete.
            previous_health_update(state="after_failed_io")
            engine.close()
            state = engine.health.snapshot()
            self.assertFalse(state["clean_shutdown"])

    def test_journal_write_error_prevents_broker_order(self):
        class FakeModule:
            def __init__(self):
                self.orders = 0
            def QUERYMARGIN(self):
                return {"Code": "0000", "Data": [{"Available": "9999999"}]}
            def NEWORDER(self, _payload):
                self.orders += 1
                return {"Code": "0000", "OrdNo": "NEW"}

        with tempfile.TemporaryDirectory() as folder:
            module = FakeModule()
            config = StrategyConfig(symbol="TXFA7", max_inventory=1, max_order_qty=1)
            gateway = TTBOrderGateway(
                module, config, PositionLedger(config), 0.25,
                risk_counters_path=Path(folder) / "rates.json",
                journal_dir=Path(folder) / "journal",
                risk_limits=PreTradeLimits(
                    max_notional=100_000_000, minimum_available_margin=1_000,
                    available_margin_field="Available",
                ),
            )
            gateway.bootstrapped = True
            with patch.object(gateway.journal, "append", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
            self.assertEqual(module.orders, 0)

    def test_stale_bot_is_not_ready_even_if_status_server_is_alive(self):
        with tempfile.TemporaryDirectory() as folder:
            config = StrategyConfig(symbol="TXFA7")
            engine = BotEngine(
                config, ShadowGateway(PositionLedger(config)), Path(folder) / "runs",
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            server = StatusServer("127.0.0.1", port, engine.health.snapshot)
            try:
                server.start()
                for state in ("starting", "running", "stale_market_data", "paused", "waiting_market", "manual_reconciliation_required", "stopped"):
                    engine.health.update(state=state)
                    with self.assertRaises(HTTPError) as error:
                        urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2)
                    self.assertEqual(error.exception.code, 503)
                engine.health.update(state="healthy")
                with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as resp:
                    self.assertTrue(json.loads(resp.read())["ready"])
            finally:
                server.close()
                engine.recorder.close()
                engine.raw_recorder.close()

    def test_status_endpoint_under_parallel_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            config = StrategyConfig(symbol="TXFA7")
            engine = BotEngine(
                config, ShadowGateway(PositionLedger(config)), Path(folder) / "runs",
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            server = StatusServer("127.0.0.1", port, engine.health.snapshot)
            try:
                server.start()
                def get_status(_index):
                    with urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as resp:
                        return json.loads(resp.read())
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    rows = list(pool.map(get_status, range(32)))
                self.assertEqual(len(rows), 32)
                self.assertTrue(all(row["symbol"] == "TXFA7" for row in rows))
            finally:
                server.close()
                engine.recorder.close()
                engine.raw_recorder.close()


if __name__ == "__main__":
    unittest.main()
