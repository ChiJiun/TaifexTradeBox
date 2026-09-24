import unittest

from monte_carlo_market_maker import synthetic_quotes
from optimize_virtual_competition import search
from strategy_core import Fill, MicropriceMarketMaker, StrategyConfig
from test_strategy_core import quote


class VirtualMaxStrategyTests(unittest.TestCase):
    def make(self, **overrides):
        values = dict(
            symbol="TXFA7",
            warmup_ticks=2,
            virtual_max_mode=True,
            fee_per_fill=0,
            tax_rate=0,
            base_qty=1,
            max_order_qty=8,
            max_inventory=8,
            quote_ttl_seconds=10,
            reprice_threshold_ticks=4,
        )
        values.update(overrides)
        return MicropriceMarketMaker(StrategyConfig(**values))

    def test_virtual_max_quotes_at_best_prices(self):
        strategy = self.make(entry_offset_ticks=0)
        strategy.decide(quote(bid_qty=100, ask_qty=100))
        decision = strategy.decide(
            quote(bid_qty=100, ask_qty=100, received_at=101)
        )
        self.assertEqual(
            {(intent.side, intent.price) for intent in decision.intents},
            {("buy", 19999), ("sell", 20000)},
        )

    def test_martingale_increases_after_loss_and_resets_after_win(self):
        strategy = self.make(sizing_mode="martingale", sizing_multiplier=2)
        strategy.decide(quote(bid_qty=100, ask_qty=100))
        initial = strategy.decide(
            quote(bid_qty=100, ask_qty=100, received_at=101)
        )
        self.assertTrue(all(intent.quantity == 1 for intent in initial.intents))

        strategy.ledger.on_fill(Fill("buy", 19999, 1, occurred_at=101))
        strategy.decide(quote(received_at=102))
        strategy.ledger.on_fill(Fill("sell", 19990, 1, occurred_at=102))
        after_loss = strategy.decide(
            quote(bid_qty=100, ask_qty=100, received_at=103)
        )
        self.assertTrue(all(intent.quantity == 2 for intent in after_loss.intents))

        strategy.ledger.on_fill(Fill("buy", 19999, 2, occurred_at=103))
        strategy.decide(quote(received_at=104))
        strategy.ledger.on_fill(Fill("sell", 20003, 2, occurred_at=104))
        after_win = strategy.decide(
            quote(bid_qty=100, ask_qty=100, received_at=105)
        )
        self.assertTrue(all(intent.quantity == 1 for intent in after_win.intents))

    def test_anti_martingale_increases_after_win(self):
        strategy = self.make(sizing_mode="anti_martingale", sizing_multiplier=2)
        strategy.decide(quote(bid_qty=100, ask_qty=100))
        strategy.decide(quote(bid_qty=100, ask_qty=100, received_at=101))
        strategy.ledger.on_fill(Fill("buy", 19999, 1, occurred_at=101))
        strategy.decide(quote(received_at=102))
        strategy.ledger.on_fill(Fill("sell", 20003, 1, occurred_at=102))
        after_win = strategy.decide(
            quote(bid_qty=100, ask_qty=100, received_at=103)
        )
        self.assertTrue(all(intent.quantity == 2 for intent in after_win.intents))


class VirtualOptimizerTests(unittest.TestCase):
    def test_profit_search_returns_ranked_cost_aware_results(self):
        days = [synthetic_quotes(120, "TXFA7", seed=seed) for seed in (1, 2)]
        base = StrategyConfig(
            symbol="TXFA7",
            fee_per_fill=100,
            tax_rate=0.00002,
            warmup_ticks=2,
            daily_loss_limit=200_000,
            max_drawdown=300_000,
        )
        report = search(
            days,
            base,
            targets=[3, 4],
            stops=[6],
            holds=[15],
            thresholds=[0.16],
            ttls=[2],
            reprices=[4],
            offsets=[0],
            sizing_modes=["fixed", "martingale"],
            multipliers=[2],
            base_quantities=[1],
            max_quantities=[1, 2],
            initial_capital=2_000_000,
            required_days=28,
            product_count=1,
            seed=7,
            objective="profit",
            top=5,
            max_combinations=100,
        )
        self.assertGreater(report["evaluated_combinations"], 1)
        results = report["top_results"]
        self.assertTrue(
            all(
                results[index]["total_net_pnl"]
                >= results[index + 1]["total_net_pnl"]
                for index in range(len(results) - 1)
            )
        )
        self.assertTrue(all(result["fees_tax"] >= 0 for result in results))
        self.assertFalse(results[0]["known_rules_met"])


if __name__ == "__main__":
    unittest.main()
