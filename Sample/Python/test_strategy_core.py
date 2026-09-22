import unittest

from strategy_core import (
    Fill,
    IndependentFillSimulator,
    IndependentFillStrategy,
    OrderIntent,
    PositionLedger,
    Quote,
    SoftRecoverySizer,
    StrategyConfig,
)


def quote(**overrides):
    values = {
        "symbol": "TXFA7",
        "bid": 19999.0,
        "ask": 20000.0,
        "bid_qty": 100.0,
        "ask_qty": 20.0,
        "last": 20000.0,
        "last_qty": 10,
        "total_volume": 1000.0,
        "exchange_time": "09:00:00.000",
        "received_at": 100.0,
    }
    values.update(overrides)
    return Quote(**values)


class StrategyCoreTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(symbol="TXFA7", point_value=200, target_ticks=3)

    def test_ttb_quote_parsing(self):
        parsed = Quote.from_ttb(
            {
                "Symbol": "TXFA7",
                "BidPs": "19,999",
                "AskPs": "20,000",
                "BidPv": "12",
                "AskPv": "8",
                "Price": "20,000",
                "Qty": "3",
                "Volume": "1,234",
                "TickTime": "09:00:00.000",
            }
        )
        self.assertEqual(parsed.bid, 19999)
        self.assertEqual(parsed.ask, 20000)
        self.assertEqual(parsed.last_qty, 3)
        self.assertTrue(parsed.is_valid())

    def test_cost_floor(self):
        config = StrategyConfig(
            symbol="TMFA7",
            point_value=10,
            fee_per_fill=8,
            tax_rate=0.00002,
            target_ticks=4,
        )
        self.assertEqual(config.estimated_round_trip_cost(40_000), 32)
        self.assertEqual(config.minimum_profitable_ticks(40_000), 4)
        self.assertEqual(config.estimated_cycle_net(40_000), 8)

    def test_soft_recovery_is_capped(self):
        sizer = SoftRecoverySizer(self.config)
        self.assertEqual(sizer.quantity(0, 20000, 0), 1)
        self.assertEqual(sizer.quantity(1000000, 20000, 0), 4)
        self.assertEqual(sizer.quantity(1000000, 20000, 7), 1)

    def test_ledger_round_trip(self):
        ledger = PositionLedger(self.config)
        ledger.on_fill(Fill("buy", 20000, 2, occurred_at=1))
        delta = ledger.on_fill(Fill("sell", 20003, 2, occurred_at=2))
        self.assertEqual(delta, 1200)
        self.assertEqual(ledger.position, 0)
        self.assertEqual(ledger.realized_pnl, 1200)

    def test_strategy_enters_with_positive_imbalance(self):
        strategy = IndependentFillStrategy(self.config)
        decision = strategy.decide(quote())
        self.assertEqual(len(decision.intents), 1)
        self.assertEqual(decision.intents[0].side, "buy")
        self.assertEqual(decision.intents[0].price, 19999)

    def test_strategy_places_profit_exit(self):
        ledger = PositionLedger(self.config)
        ledger.on_fill(Fill("buy", 20000, 1, occurred_at=90))
        strategy = IndependentFillStrategy(self.config, ledger)
        decision = strategy.decide(quote(last=20001, bid=20000, ask=20001))
        self.assertEqual(decision.intents[0].side, "sell")
        self.assertEqual(decision.intents[0].price, 20003)
        self.assertEqual(decision.intents[0].role, "exit")

    def test_simulator_requires_a_later_tick(self):
        simulator = IndependentFillSimulator()
        simulator.sync((OrderIntent("buy", 19999, 2, "entry", "test"),), now=100)
        self.assertEqual(simulator.on_quote(quote(last=19999, received_at=100)), [])
        fills = simulator.on_quote(quote(last=19999, last_qty=1, total_volume=1001, received_at=101))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, 1)


if __name__ == "__main__":
    unittest.main()
