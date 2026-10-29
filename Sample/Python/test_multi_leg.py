from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sim_hft_bot
from ttb.api import SharedReportsApi, TTBApi


class FakeModule:
    def __init__(self, positions=None):
        self.order_queries = 0
        self.position_queries = 0
        self.positions = positions or []

    def QUOTEDATA(self, symbols):
        return {"Code": "000"}

    def QUERYRESTOREREPORT(self):
        self.order_queries += 1
        return {"Code": "0000", "Data": []}

    def QUERYRESTOREFILLREPORT(self):
        self.position_queries += 1
        return {"Code": "0000", "Data": self.positions}


class SilentMonitor:
    def __init__(self, api, port, symbol, callback, **_kwargs):
        self.symbol = symbol

    def start(self):
        pass

    def close(self):
        pass


class LegParsingTests(unittest.TestCase):
    def test_a_leg_inherits_the_primary_line_and_overrides_what_it_lists(self):
        legs = sim_hft_bot.leg_namespaces([
            "--symbol", "CCFK6", "--tick-size", "0.5", "--max-inventory", "8", "--fee-per-fill", "50",
            "--leg-json", json.dumps(["--symbol", "CDFK6", "--tick-size", "5", "--max-inventory", "3"]),
        ])
        self.assertEqual([leg.symbol for leg in legs], ["CCFK6", "CDFK6"])
        self.assertEqual([leg.tick_size for leg in legs], [0.5, 5.0])
        self.assertEqual([leg.max_inventory for leg in legs], [8, 3])
        self.assertEqual(legs[1].fee_per_fill, 50.0)

    def test_each_leg_gets_its_own_files_and_the_primary_health_path_stays_combined(self):
        legs = sim_hft_bot.leg_namespaces(["--symbol", "auto:CCF", "--leg-json", '["--symbol", "auto:CDF"]'])
        self.assertEqual(legs[0].health_file, Path("output") / "health.json")
        self.assertEqual(legs[0].leg_health_file, Path("output") / "health-AUTO_CCF.json")
        self.assertEqual(legs[1].output, Path("output") / "runs" / "AUTO_CDF")
        self.assertEqual(legs[1].risk_state_file, Path("output") / "risk-state-AUTO_CDF.json")

    def test_a_single_leg_keeps_its_original_paths(self):
        (leg,) = sim_hft_bot.leg_namespaces(["--symbol", "CCFK6"])
        self.assertEqual(leg.output, Path("output") / "runs")
        self.assertEqual(leg.risk_state_file, Path("output") / "risk-state.json")
        self.assertFalse(hasattr(leg, "leg_health_file"))

    def test_bad_or_nested_leg_json_is_refused(self):
        for raw in ("not json", '{"--symbol": "CDFK6"}', '["--leg-json", "[]"]'):
            with self.subTest(raw=raw), self.assertRaises(SystemExit):
                sim_hft_bot.leg_namespaces(["--symbol", "CCFK6", "--leg-json", raw])


class CombinedHealthTests(unittest.TestCase):
    def leg(self, symbol, state, position=0, quote="", error=""):
        return {"symbol": symbol, "state": state, "position": position, "mode": "ttb",
                "last_quote_wall_utc": quote, "fatal_error": error, "clean_shutdown": False}

    def test_any_failed_leg_fails_the_bot(self):
        result = sim_hft_bot.combined_health([
            self.leg("CCFK6", "healthy"), self.leg("CDFK6", "error", error="boom"),
        ])
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["fatal_error"], "CDFK6: boom")

    def test_a_paused_sparse_leg_does_not_hide_a_trading_leg(self):
        result = sim_hft_bot.combined_health([
            self.leg("CCFK6", "healthy", 2, "2026-10-30T01:00:01+00:00"),
            self.leg("CDFK6", "stale_market_data", -1, "2026-10-30T00:50:00+00:00"),
        ])
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["position"], 3)
        self.assertEqual(result["last_quote_wall_utc"], "2026-10-30T01:00:01+00:00")
        self.assertEqual(result["symbol"], "CCFK6,CDFK6")

    def test_unknown_position_on_any_leg_stays_unknown(self):
        result = sim_hft_bot.combined_health([self.leg("CCFK6", "waiting_market"), self.leg("CDFK6", "starting", None)])
        self.assertIsNone(result["position"])
        self.assertEqual(result["state"], "waiting_market")


class SharedReportsTests(unittest.TestCase):
    def test_legs_polling_together_share_one_fetch_until_it_ages_out(self):
        now = [100.0]
        module = FakeModule()
        api = SharedReportsApi(module, max_age=1.0, clock=lambda: now[0])
        api.query_orders(); api.query_orders(); api.query_positions(); api.query_positions()
        self.assertEqual((module.order_queries, module.position_queries), (1, 1))
        now[0] += 1.0
        api.query_orders()
        self.assertEqual(module.order_queries, 2)


class MultiLegMainTests(unittest.TestCase):
    def run_main(self, root: Path, module: FakeModule, mode_args: list[str]) -> int:
        stop = root / "stop.requested"
        stop.write_text("stop", encoding="utf-8")
        argv = [
            "--symbol", "CCFK6", "--tick-size", "0.5", "--point-value", "2000", "--status-port", "0",
            "--output", str(root / "runs"), "--health-file", str(root / "health.json"),
            "--risk-state-file", str(root / "risk-state.json"), "--stop-file", str(stop),
            "--rate-counters-file", str(root / "rate.json"), "--journal-dir", str(root / "journal"),
            "--leg-json", json.dumps(["--symbol", "CDFK6", "--tick-size", "5",
                                      "--rate-counters-file", str(root / "rate-cdf.json")]),
        ] + mode_args
        with patch.object(sim_hft_bot.TTBApi, "connect", return_value=TTBApi(module)), \
                patch.object(sim_hft_bot, "QuoteMonitor", SilentMonitor), \
                patch.object(sim_hft_bot.signal, "signal"):
            return sim_hft_bot.main(argv)

    def test_shadow_legs_run_side_by_side_and_write_a_combined_health_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_main(root, FakeModule(), []), 0)
            combined = json.loads((root / "health.json").read_text(encoding="utf-8"))
            self.assertEqual(combined["symbol"], "CCFK6,CDFK6")
            self.assertEqual([leg["symbol"] for leg in combined["legs"]], ["CCFK6", "CDFK6"])
            self.assertTrue((root / "health-CCFK6.json").exists())
            self.assertTrue((root / "health-CDFK6.json").exists())
            self.assertTrue((root / "runs" / "CDFK6").is_dir())

    def test_live_legs_refuse_to_start_on_a_non_flat_account_before_any_order(self):
        module = FakeModule(positions=[{"ComdID1": "DHFK6", "ComdBS1": "B", "Hold": "1"}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "Non-flat"):
                self.run_main(root, module, [
                    "--mode", "ttb", "--confirm-virtual-orders", "--confirm-flat-account",
                    "--max-notional", "5000000", "--available-margin-field", "AVAILABELMARGIN",
                    "--min-available-margin", "60000",
                ])
            combined = json.loads((root / "health.json").read_text(encoding="utf-8"))
            self.assertEqual(combined["state"], "manual_reconciliation_required")
            self.assertIn("account baseline refused", combined["fatal_error"])


if __name__ == "__main__":
    unittest.main()
