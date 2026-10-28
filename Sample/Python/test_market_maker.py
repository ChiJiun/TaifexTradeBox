import unittest
from dataclasses import asdict

from strategy_core import StrategyConfig, MicropriceMarketMaker, IndependentFillSimulator, OrderIntent, Fill
from test_strategy_core import quote
from replay_market_maker import replay
from sim_hft_bot import TTBOrderGateway, _float


class MarketMakerTests(unittest.TestCase):
    def make(self, **kwargs):
        return MicropriceMarketMaker(StrategyConfig(symbol="TXFA7", warmup_ticks=2, target_ticks=8, **kwargs))

    def test_neutral_two_sides_and_no_same_side_add(self):
        s = self.make()
        s.decide(quote(bid_qty=100, ask_qty=100))
        d = s.decide(quote(bid_qty=100, ask_qty=100, received_at=101))
        self.assertEqual({x.side for x in d.intents}, {"buy", "sell"})
        s.ledger.on_fill(Fill("buy", 19995, 1, occurred_at=101))
        d = s.decide(quote(received_at=102))
        self.assertEqual([(x.side, x.quantity) for x in d.intents], [("sell", 1)])
        self.assertEqual(d.intents[0].price, 20003)

    def test_unrealized_loss_halts_and_exits(self):
        s = self.make(daily_loss_limit=500)
        s.ledger.on_fill(Fill("buy", 20010, 1, occurred_at=100))
        d = s.decide(quote(received_at=101))
        self.assertTrue(d.halted)
        self.assertEqual(d.intents[0].role, "risk_exit")
        s.ledger.on_fill(Fill("sell", 19999, 1, occurred_at=101))
        self.assertEqual(s.decide(quote(received_at=102)).intents, ())

    def test_quote_duplicates_do_not_fill(self):
        sim = IndependentFillSimulator()
        sim.on_quote(quote())
        sim.sync([OrderIntent("buy", 20000, 2, "entry", "test")], 100)
        self.assertEqual(sim.on_quote(quote(received_at=101)), [])
        fills = sim.on_quote(quote(last=19998, last_qty=1, total_volume=1001, received_at=102))
        self.assertEqual(fills[0].price, 20000)
        self.assertEqual(fills[0].quantity, 1)
        self.assertEqual(sim.on_quote(quote(last=19998, total_volume=1001, received_at=103)), [])

    def test_cost_filter_and_warmup(self):
        s = self.make(fee_per_fill=10000)
        self.assertEqual(s.decide(quote()).reason, "warmup")
        self.assertEqual(s.decide(quote(received_at=101)).reason, "cost filter")

    def test_entry_quotes_are_sticky_until_reprice_threshold(self):
        s = self.make(quote_ttl_seconds=10, reprice_threshold_ticks=4)
        s.decide(quote(bid_qty=100, ask_qty=100))
        initial = s.decide(quote(bid_qty=100, ask_qty=100, received_at=101))
        moved_one = s.decide(
            quote(
                bid=20000,
                ask=20001,
                last=20000,
                bid_qty=100,
                ask_qty=100,
                received_at=102,
            )
        )
        self.assertEqual(
            [(intent.side, intent.price) for intent in initial.intents],
            [(intent.side, intent.price) for intent in moved_one.intents],
        )
        moved_five = s.decide(
            quote(
                bid=20004,
                ask=20005,
                last=20004,
                bid_qty=100,
                ask_qty=100,
                received_at=103,
            )
        )
        self.assertNotEqual(
            [(intent.side, intent.price) for intent in initial.intents],
            [(intent.side, intent.price) for intent in moved_five.intents],
        )

    def test_replay_rejects_reverse_time(self):
        with self.assertRaises(ValueError):
            replay([asdict(quote(received_at=102)), asdict(quote(received_at=101))], self.make().config)

    def test_signed_number(self):
        self.assertEqual(_float("-1,200.5"), -1200.5)

    def test_full_replay_cycle_accounts_for_costs(self):
        config = self.make(fee_per_fill=100, tax_rate=.00002).config
        rows = [
            quote(bid_qty=100, ask_qty=100, received_at=100),
            quote(bid_qty=100, ask_qty=100, received_at=101),
            quote(bid=19995, ask=19996, last=19995, bid_qty=100, ask_qty=100,
                  total_volume=1001, last_qty=1, received_at=102),
            quote(bid=20003, ask=20004, last=20003, bid_qty=100, ask_qty=100,
                  total_volume=1002, last_qty=1, received_at=103),
        ]
        result = replay([asdict(x) for x in rows], config)
        self.assertEqual(result["filled_contracts"], 2)
        self.assertEqual(result["open_position"], 0)
        self.assertAlmostEqual(result["realized_net"], 1600 - 200 - (19995 + 20003) * 200 * .00002)

    def test_open_inventory_reserves_exit_cost_and_reports_return(self):
        config = self.make(fee_per_fill=100, tax_rate=.00002).config
        rows = [
            quote(bid_qty=100, ask_qty=100, received_at=100),
            quote(bid_qty=100, ask_qty=100, received_at=101),
            quote(
                bid=19995,
                ask=19996,
                last=19995,
                bid_qty=100,
                ask_qty=100,
                total_volume=1001,
                last_qty=1,
                received_at=102,
            ),
        ]
        result = replay([asdict(x) for x in rows], config, initial_capital=1_000_000)
        expected_exit_cost = 100 + 19995 * 200 * .00002
        self.assertEqual(result["open_position"], 1)
        self.assertAlmostEqual(result["estimated_exit_cost"], expected_exit_cost)
        self.assertAlmostEqual(
            result["cumulative_return"],
            result["marked_equity"] / 1_000_000,
        )

    def test_invalid_quote_is_rejected(self):
        self.assertFalse(quote(bid=float("nan")).is_valid())
        self.assertTrue(self.make().decide(quote(symbol="OTHER")).halted)

    def test_cancel_ack_does_not_allow_replacement(self):
        class Module:
            def CANCELORDER(self, payload):
                return {"Code": "0000"}
            def QUERYRESTOREREPORT(self):
                return {"Code": "0000", "Data": []}

            def NEWORDER(self, payload):
                raise AssertionError("replacement before terminal report")
        s = self.make()
        g = TTBOrderGateway(Module(), s.config, s.ledger, .5)
        g.bootstrapped = True
        g.active["buy"] = {"order_no": "a", "price": 19990, "qty": 1}
        g.sync((OrderIntent("buy", 19991, 1, "entry", "test"),), quote())
        self.assertIn("buy", g.active)
        self.assertIn("a", g.cancel_requested)


if __name__ == "__main__":
    unittest.main()
