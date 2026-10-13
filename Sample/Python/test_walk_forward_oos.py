from __future__ import annotations

import csv
import hashlib
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from monte_carlo_market_maker import synthetic_quotes
from replay_market_maker import replay
from strategy_core import StrategyConfig
from walk_forward_oos import (
    ResearchGates, aggregate, analyze, markdown_report,
    resolve_days, synthetic_days, verify_manifest, main as research_main,
)


def config() -> StrategyConfig:
    return StrategyConfig(
        symbol="TMFJ6", point_value=10, warmup_ticks=2,
        half_width_ticks=2, fee_per_fill=20.0, tax_rate=0.00002,
        slippage_ticks=1, max_inventory=1, max_order_qty=1,
        virtual_max_mode=True, recovery_fraction=0,
    )


def research(days):
    return analyze(
        days, config(), candidates=[(4, 2), (6, 2), (8, 4)],
        gates=ResearchGates(min_closed_cycles=100),
        verified_source=False, source_description="synthetic unit test",
        holdout_days=2, initial_train_days=3,
        mc_trials=3, block_size=8, seed=7,
    )


class WalkForwardResearchTests(unittest.TestCase):
    def test_strict_date_split_and_synthetic_never_passes(self):
        days = synthetic_days(8, "TMFJ6", 45, 7)
        report = research(days)
        self.assertEqual(report["split"]["preholdout_dates"], [x[0] for x in days[:-2]])
        self.assertEqual(report["split"]["final_holdout_dates"], [x[0] for x in days[-2:]])
        self.assertFalse(report["production_research_gate_pass"])
        self.assertFalse(report["acceptance_checks"]["verified_real_source"])
        self.assertEqual(len(report["walk_forward_folds"]), 3)
        for fold in report["walk_forward_folds"]:
            self.assertTrue(all(x < fold["validation_date"] for x in fold["training_dates"]))
        self.assertEqual(set(report["final_holdout_by_fill_model"]),
                         {"independent", "probabilistic", "queue"})
        self.assertIn("FAIL", markdown_report(report))
        self.assertIn("adverse selection", markdown_report(report))

    def test_changed_final_holdout_never_changes_training_selection(self):
        original = synthetic_days(8, "TMFJ6", 35, 12)
        changed = list(original)
        changed[-2:] = [
            (d, synthetic_quotes(35, "TMFJ6", seed=998 + i))
            for i, (d, rows) in enumerate(original[-2:])
        ]
        first, second = research(original), research(changed)
        self.assertEqual(first["walk_forward_folds"], second["walk_forward_folds"])
        self.assertEqual(first["selected_parameters"], second["selected_parameters"])
        self.assertEqual(first["sensitivity_preholdout_only"], second["sensitivity_preholdout_only"])

    def test_replay_extra_metrics_and_slippage_adjustment(self):
        rows = synthetic_quotes(120, "TMFJ6", seed=27)
        cfg = config()
        without = replay(rows, replace(cfg, slippage_ticks=0))
        with_cost = replay(rows, cfg)
        self.assertEqual(without["filled_contracts"], with_cost["filled_contracts"])
        self.assertAlmostEqual(with_cost["net_marked_equity"],
                               without["net_marked_equity"] - with_cost["slippage_cost"])
        self.assertEqual(set(with_cost["adverse_selection"]), {"1", "2", "5", "10"})
        self.assertGreaterEqual(with_cost["submitted_orders"], with_cost["filled_orders"])
        self.assertIn("inventory_hold_seconds_p95", with_cost)
        summarized = aggregate([{"date": "2026-01-01", **with_cost},
                                {"date": "2026-01-02", **with_cost}])
        self.assertEqual(summarized["days"], 2)

    def test_nonchrono_duplicate_and_synthetic_marked_csv_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = synthetic_quotes(8, "TMFJ6")
            def save(day, data, suffix="090000"):
                path = root / f"quotes-{day}-{suffix}.csv"
                with path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=data[0].keys())
                    writer.writeheader()
                    writer.writerows(data)
                return path
            files = []
            for i in range(6):
                transformed = [dict(r, exchange_time="09:00:00") for r in rows]
                files.append(save(f"202609{(i+1):02d}", transformed))
            self.assertEqual(len(resolve_days(files, "TMFJ6")), 6)
            with self.assertRaisesRegex(ValueError, "multiple quote files"):
                resolve_days(files + [save("20260901", [dict(r, exchange_time="09:00:00") for r in rows], "100000")], "TMFJ6")
            files[0] = save("20260901", rows)
            with self.assertRaisesRegex(ValueError, "synthetic quote marker"):
                resolve_days(files, "TMFJ6")
            corrupt = [dict(r, exchange_time="09:00:00") for r in rows]
            corrupt[2]["received_at"] = corrupt[0]["received_at"]
            files[0] = save("20260901", corrupt)
            with self.assertRaisesRegex(ValueError, "out-of-order"):
                resolve_days(files, "TMFJ6")

    def test_operator_manifest_must_bind_exact_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root / "quotes-20260901-090000.csv"
            file.write_text("test bytes", encoding="utf-8")
            manifest = root / "source.json"
            manifest.write_text(json.dumps({
                "source": "verified_ttb_quotes", "trading_day_reconciled": True,
                "reviewer": "manual-review",
                "reviewed_at": "2026-10-13",
                "files": {file.name: hashlib.sha256(file.read_bytes()).hexdigest()},
            }), encoding="utf-8")
            self.assertTrue(verify_manifest([file], manifest))
            file.write_text("changed bytes", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_manifest([file], manifest)

    def test_acceptance_config_must_not_weaken_numeric_limits(self):
        dates = synthetic_days(6, "TMFJ6", 15, 3)
        variants = [
            ResearchGates(min_closed_cycles=99),
            ResearchGates(max_drawdown_ntd=300001),
            ResearchGates(max_worst_day_loss_ntd=200001),
            ResearchGates(max_one_day_profit_share=0.51),
            ResearchGates(max_mc_loss_probability=0.101),
            ResearchGates(min_fill_rate=0.005),
            ResearchGates(max_drawdown_ntd=math.inf),
            ResearchGates(max_drawdown_ntd=math.nan),
        ]
        for gates in variants:
            with self.subTest(gates=gates), self.assertRaisesRegex(ValueError, "acceptance gates"):
                analyze(
                    dates, config(), candidates=[(4, 2)], gates=gates,
                    verified_source=False, source_description="synthetic test",
                    holdout_days=2, initial_train_days=3,
                    mc_trials=1, block_size=4, seed=1,
                )

    def test_cli_creates_json_markdown_and_synthetic_fails_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "results"
            code = research_main([
                "--symbol", "TMFJ6",
                "--synthetic-days", "7", "--quotes-per-day", "20",
                "--fee-per-fill", "20", "--tax-rate", "0.00002",
                "--slippage-ticks", "1", "--mc-trials", "2",
                "--target-ticks", "4", "--half-width-ticks", "2",
                "--output", str(output),
            ])
            self.assertEqual(code, 0)
            data = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            report = output.with_suffix(".md").read_text(encoding="utf-8")
            self.assertFalse(data["production_research_gate_pass"])
            self.assertEqual(len(data["split"]["final_holdout_dates"]), 2)
            self.assertIn("FAIL", report)
            self.assertIn("probabilistic", report)

    def test_too_few_days_and_nonpositive_holdout_fail(self):
        days = synthetic_days(7, "TMFJ6", 30, 2)
        with self.assertRaises(ValueError):
            analyze(days, config(), candidates=[(4, 2)], gates=ResearchGates(),
                    verified_source=False, source_description="test",
                    holdout_days=4, initial_train_days=3,
                    mc_trials=2, block_size=3, seed=1)


if __name__ == "__main__":
    unittest.main()
