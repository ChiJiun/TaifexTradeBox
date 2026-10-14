from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from automation.shadow_portfolio import (
    PortfolioSafetyError, PortfolioRiskState, child_command, evaluate_portfolio, load_config,
    request_global_stop, run, runtime_paths, persist_portfolio_health,
    awaiting_initial_quotes,
)
from strategy_core import PositionLedger, StrategyConfig
from ttb.engine import BotEngine
from ttb.orders import ShadowGateway
from ttb.status_server import StatusServer


class IsolatedPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.manifest = self.root / "portfolio.json"
        example = Path(__file__).parent / "automation" / "shadow_portfolio.example.json"
        self.data = json.loads(example.read_text(encoding="utf-8"))
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def config(self):
        return load_config(self.manifest)

    def test_two_symbols_get_disjoint_ports_paths_state_and_commands(self):
        settings = self.config()
        first, second = settings.instances
        self.assertNotEqual(first.symbol, second.symbol)
        self.assertNotEqual(first.status_port, second.status_port)
        p1, p2 = runtime_paths(self.output, first), runtime_paths(self.output, second)
        for key in ("health", "risk", "journal", "rates", "stop", "output", "log"):
            self.assertNotEqual(p1[key], p2[key])
        cmd = child_command(first, self.output, "python")
        self.assertIn("--mode", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "shadow")
        self.assertIn("--status-port", cmd)
        self.assertEqual(cmd[cmd.index("--status-port") + 1], str(first.status_port))
        self.assertEqual(cmd[cmd.index("--health-file") + 1], str(p1["health"]))
        self.assertEqual(cmd[cmd.index("--risk-state-file") + 1], str(p1["risk"]))

    def test_portfolio_disallows_live_duplicate_symbol_port_and_flag_override(self):
        for edit in ("live", "duplicate_port", "duplicate_symbol", "override_mode"):
            obj = json.loads(json.dumps(self.data))
            if edit == "live":
                obj["instances"][0]["mode"] = "ttb"
            elif edit == "duplicate_port":
                obj["instances"][1]["status_port"] = obj["instances"][0]["status_port"]
            elif edit == "duplicate_symbol":
                obj["instances"][1]["symbol"] = obj["instances"][0]["symbol"]
            else:
                obj["instances"][0]["extra_args"] = ["--mode", "ttb"]
            self.manifest.write_text(json.dumps(obj), encoding="utf-8")
            with self.subTest(edit=edit), self.assertRaises(PortfolioSafetyError):
                self.config()

    def write_health(self, instance, *, position, price, pnl, state="healthy", age=0):
        path = runtime_paths(self.output, instance)["health"]
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "symbol": instance.symbol, "mode": "shadow", "state": state,
            "position": position, "last_price": price, "marked_pnl": pnl,
            "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
            "trading_day": "2026-10-14",
            "last_quote_wall_utc": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_fresh_heartbeat_cannot_hide_stale_exchange_quote(self):
        settings = self.config()
        first, second = settings.instances
        self.write_health(first, position=1, price=20000, pnl=-100)
        self.write_health(second, position=0, price=20000, pnl=0)
        file = runtime_paths(self.output, first)["health"]
        row = json.loads(file.read_text(encoding="utf-8"))
        row["last_quote_wall_utc"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        # Heartbeat remains fresh, but trading risk is UNVALUED.
        file.write_text(json.dumps(row), encoding="utf-8")
        with self.assertRaisesRegex(PortfolioSafetyError, "invalid/stale"):
            evaluate_portfolio(settings, self.output, now=datetime.now(timezone.utc))
        with self.assertRaisesRegex(PortfolioSafetyError, "bad initial health"):
            awaiting_initial_quotes(settings, self.output, datetime.now(timezone.utc))

    def test_flat_market_closed_old_quote_waits_without_risk_pass(self):
        settings = self.config()
        first, second = settings.instances
        self.write_health(first, position=0, price=20000, pnl=0, state="waiting_market")
        self.write_health(second, position=0, price=20000, pnl=0, state="waiting_market")
        file = runtime_paths(self.output, first)["health"]
        row = json.loads(file.read_text(encoding="utf-8"))
        row["last_quote_wall_utc"] = (
            datetime.now(timezone.utc) - timedelta(hours=6)
        ).isoformat()
        file.write_text(json.dumps(row), encoding="utf-8")
        self.assertTrue(awaiting_initial_quotes(settings, self.output, datetime.now(timezone.utc)))
        with self.assertRaises(PortfolioSafetyError):
            evaluate_portfolio(settings, self.output, now=datetime.now(timezone.utc))

    def test_portfolio_global_losses_drawdown_notional_and_stale_fail_closed(self):
        settings = self.config()
        first, second = settings.instances
        self.write_health(first, position=1, price=20000, pnl=-1000)
        self.write_health(second, position=-1, price=20000, pnl=-2000)
        state = evaluate_portfolio(settings, self.output, now=datetime.now(timezone.utc))
        self.assertEqual(state["marked_pnl_ntd"], -3000)
        self.assertEqual(state["gross_notional_ntd"], 1_200_000)
        self.assertEqual(state["kill_reason"], "")
        self.write_health(first, position=1, price=20000, pnl=-18000)
        self.write_health(second, position=-1, price=20000, pnl=-3000)
        state = evaluate_portfolio(settings, self.output, now=datetime.now(timezone.utc))
        self.assertEqual(state["kill_reason"], "portfolio_daily_loss_limit")
        self.write_health(first, position=0, price=20000, pnl=0)
        self.write_health(second, position=0, price=20000, pnl=-1000)
        state = evaluate_portfolio(settings, self.output,
                                   now=datetime.now(timezone.utc), peak_equity=15000)
        self.assertEqual(state["kill_reason"], "portfolio_max_drawdown")
        self.write_health(second, position=0, price=20000, pnl=0, age=3600)
        with self.assertRaisesRegex(PortfolioSafetyError, "invalid/stale health"):
            evaluate_portfolio(settings, self.output, now=datetime.now(timezone.utc))

    def test_risk_peak_persists_and_corruption_or_config_change_blocks_restart(self):
        settings = self.config()
        path = self.output / "portfolio-risk-state.json"
        state = PortfolioRiskState(path, settings)
        state.checkpoint("2026-10-14", 12500)
        reopened = PortfolioRiskState(path, settings)
        self.assertEqual(reopened.peak_for("2026-10-14"), 12500)
        self.assertEqual(reopened.peak_for("2026-10-15"), 0)
        with self.assertRaisesRegex(PortfolioSafetyError, "moved backward"):
            reopened.peak_for("2026-10-13")
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(PortfolioSafetyError, "manual recovery"):
            PortfolioRiskState(path, settings)

    def test_inconsistent_symbol_trading_days_halt(self):
        settings = self.config()
        first, second = settings.instances
        self.write_health(first, position=0, price=20000, pnl=0)
        self.write_health(second, position=0, price=20000, pnl=0)
        file = runtime_paths(self.output, second)["health"]
        data = json.loads(file.read_text(encoding="utf-8"))
        data["trading_day"] = "2026-10-15"
        file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(PortfolioSafetyError, "inconsistent trading days"):
            evaluate_portfolio(settings, self.output, now=datetime.now(timezone.utc))

    def test_waiting_for_first_shadow_quotes_never_claims_risk_pass(self):
        settings = self.config()
        for instance in settings.instances:
            file = runtime_paths(self.output, instance)["health"]
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({
                "symbol": instance.symbol, "mode": "shadow",
                "state": "waiting_market", "position": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
        self.assertTrue(awaiting_initial_quotes(
            settings, self.output, datetime.now(timezone.utc)
        ))
        first = settings.instances[0]
        self.write_health(first, position=0, price=20000, pnl=0)
        self.assertTrue(awaiting_initial_quotes(
            settings, self.output, datetime.now(timezone.utc)
        ))
        second = settings.instances[1]
        self.write_health(second, position=0, price=20000, pnl=0)
        self.assertFalse(awaiting_initial_quotes(
            settings, self.output, datetime.now(timezone.utc)
        ))

    def test_aggregate_health_written_atomically(self):
        payload = {"trading_day": "2026-10-14", "kill_reason": "",
                   "marked_pnl_ntd": 20}
        path = persist_portfolio_health(self.output, payload)
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["mode"], "shadow_portfolio")
        self.assertEqual(stored["marked_pnl_ntd"], 20)
        self.assertFalse((self.output / "portfolio-health.tmp").exists())

    def test_global_stop_persists_reason_and_all_instance_flags(self):
        settings = self.config()
        request_global_stop(settings, self.output, "portfolio_daily_loss_limit")
        self.assertIn("daily_loss", (self.output / "portfolio-halt.flag").read_text())
        for instance in settings.instances:
            self.assertTrue(runtime_paths(self.output, instance)["stop"].exists())
        with self.assertRaises(FileExistsError):
            request_global_stop(settings, self.output, "unsafe reset")

    def test_unexpected_shadow_child_exit_latches_every_instance(self):
        settings = self.config()
        class FakeChild:
            def poll(self):
                return 1
            def wait(self, timeout):
                return 1
        with patch("automation.shadow_portfolio.subprocess.Popen",
                   return_value=FakeChild()) as start:
            with self.assertRaisesRegex(PortfolioSafetyError, "child process exited"):
                run(settings, self.output, interval=1)
        self.assertEqual(start.call_count, 2)
        for item in settings.instances:
            self.assertTrue(runtime_paths(self.output, item)["stop"].exists())

    def test_independent_shadow_engines_and_status_endpoints(self):
        settings = self.config()
        engines = []
        servers = []
        try:
            for index, instance in enumerate(settings.instances):
                paths = runtime_paths(self.output, instance)
                with socket.socket() as test_socket:
                    test_socket.bind(("127.0.0.1", 0))
                    free_port = test_socket.getsockname()[1]
                cfg = StrategyConfig(symbol=instance.symbol, point_value=instance.point_value)
                engine = BotEngine(
                    cfg, ShadowGateway(PositionLedger(cfg)),
                    paths["output"], health_path=paths["health"],
                    risk_state_path=paths["risk"],
                )
                engines.append(engine)
                server = StatusServer("127.0.0.1", free_port, engine.health.snapshot)
                server.start()
                servers.append((server, free_port, instance.symbol))
            engines[0].health.update(state="healthy", marked_pnl=500)
            engines[1].health.update(state="paused", marked_pnl=-100)
            for server, port, symbol in servers:
                with urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as response:
                    row = json.loads(response.read())
                self.assertEqual(row["symbol"], symbol)
            self.assertEqual(engines[0].health.snapshot()["marked_pnl"], 500)
            self.assertEqual(engines[1].health.snapshot()["marked_pnl"], -100)
            self.assertTrue(all(runtime_paths(self.output, x)["health"].exists()
                                for x in settings.instances))
        finally:
            for server, _, _ in servers:
                server.close()
            for engine in engines:
                engine.recorder.close()
                engine.raw_recorder.close()


if __name__ == "__main__":
    unittest.main()
