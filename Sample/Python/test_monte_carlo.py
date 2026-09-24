import unittest

from monte_carlo_market_maker import block_bootstrap, run_monte_carlo, synthetic_quotes
from strategy_core import (
    OrderIntent,
    ProbabilisticFillSimulator,
    QueueAwareFillSimulator,
    StrategyConfig,
)
from test_strategy_core import quote


class FillModelTests(unittest.TestCase):
    def test_probabilistic_zero_rejects_touch_but_accepts_trade_through(self):
        simulator = ProbabilisticFillSimulator(touch_fill_probability=0.0, seed=1)
        simulator.on_quote(quote())
        simulator.sync((OrderIntent("buy", 19999, 1, "entry", "test"),), 100)
        self.assertEqual(
            simulator.on_quote(quote(last=19999, total_volume=1001, received_at=101)),
            [],
        )
        fills = simulator.on_quote(
            quote(last=19998, total_volume=1002, received_at=102)
        )
        self.assertEqual(fills[0].quantity, 1)

    def test_queue_volume_is_consumed_before_fill(self):
        simulator = QueueAwareFillSimulator(queue_fraction=1.0)
        baseline = quote(bid_qty=5, total_volume=1000)
        simulator.on_quote(baseline)
        simulator.sync(
            (OrderIntent("buy", 19999, 1, "entry", "test"),),
            100,
            baseline,
        )
        self.assertEqual(
            simulator.on_quote(
                quote(bid_qty=5, last=19999, last_qty=5, total_volume=1005, received_at=101)
            ),
            [],
        )
        fills = simulator.on_quote(
            quote(bid_qty=5, last=19999, last_qty=1, total_volume=1006, received_at=102)
        )
        self.assertEqual(fills[0].quantity, 1)

    def test_latency_requires_arrival_after_activation(self):
        simulator = ProbabilisticFillSimulator(
            touch_fill_probability=1.0, latency_ms=100, seed=1
        )
        simulator.on_quote(quote())
        simulator.sync((OrderIntent("buy", 19999, 1, "entry", "test"),), 100)
        self.assertEqual(
            simulator.on_quote(
                quote(last=19999, total_volume=1001, received_at=100.05)
            ),
            [],
        )
        fills = simulator.on_quote(
            quote(last=19999, total_volume=1002, received_at=100.11)
        )
        self.assertEqual(fills[0].quantity, 1)


class MonteCarloTests(unittest.TestCase):
    def test_block_bootstrap_is_valid_and_reproducible(self):
        rows = synthetic_quotes(80, "TXFA7", seed=4)
        first = block_bootstrap(rows, block_size=10, seed=9)
        second = block_bootstrap(rows, block_size=10, seed=9)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(rows))
        self.assertTrue(
            all(first[index]["received_at"] > first[index - 1]["received_at"] for index in range(1, len(first)))
        )
        self.assertTrue(all(row["ask"] > row["bid"] > 0 for row in first))

    def test_small_monte_carlo_runs_all_models(self):
        rows = synthetic_quotes(120, "TXFA7", seed=2)
        config = StrategyConfig(
            symbol="TXFA7",
            warmup_ticks=2,
            target_ticks=4,
            fee_per_fill=0,
            tax_rate=0,
            max_inventory=1,
            max_order_qty=1,
        )
        report = run_monte_carlo(
            rows,
            config,
            models=("independent", "probabilistic", "queue"),
            trials=3,
            block_size=20,
            touch_fill_probability=0.35,
            queue_fraction=1.0,
            latency_ms=10,
            seed=3,
        )
        self.assertEqual(set(report["models"]), {"independent", "probabilistic", "queue"})
        self.assertEqual(report["models"]["queue"]["summary"]["trials"], 3)


if __name__ == "__main__":
    unittest.main()
