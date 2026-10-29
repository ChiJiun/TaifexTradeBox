import unittest

from strategy_core import Fill, PositionLedger, Quote, StrategyConfig
from strategies.flow_momentum import FlowMomentumStrategy


def tick(t, last, volume, bid=None, ask=None):
    bid = last - 1 if bid is None else bid
    ask = last + 1 if ask is None else ask
    return Quote(symbol="TMFK6", bid=bid, ask=ask, bid_qty=5, ask_qty=5, last=last, last_qty=1,
                 total_volume=volume, exchange_time="10:00:00", received_at=t)


class FlowMomentumTests(unittest.TestCase):
    def make(self, **settings):
        config = StrategyConfig(symbol="TMFK6", point_value=10, fee_per_fill=50, tax_rate=0.00002,
                                base_qty=1, max_order_qty=1, max_inventory=1)
        self.ledger = PositionLedger(config)
        defaults = dict(window_seconds=30, flow_threshold=20, move_ticks=5, cooldown_seconds=0,
                        target_ticks=30, stop_ticks=20, max_hold_seconds=120)
        defaults.update(settings)
        return FlowMomentumStrategy(config, self.ledger, **defaults)

    def push_up(self, strategy, start=1000.0, price=46500, volume=100):
        decision = strategy.decide(tick(start, price, volume))
        for i in range(1, 4):
            # Each print is an uptick: buying by the tick rule.
            decision = strategy.decide(tick(start + i, price + 3 * i, volume + 10 * i))
        return decision

    def test_aggressive_buying_triggers_a_crossing_buy(self):
        decision = self.push_up(self.make())
        (intent,) = decision.intents
        self.assertEqual((intent.side, intent.role), ("buy", "entry"))
        self.assertEqual(intent.price, 46506 + 1 + 2)

    def test_zero_tick_prints_keep_the_last_direction(self):
        strategy = self.make(move_ticks=1)
        strategy.decide(tick(1000.0, 46500, 100))
        strategy.decide(tick(1001.0, 46498, 110))
        # Same price, more volume: still selling by the tick rule.
        (intent,) = strategy.decide(tick(1002.0, 46498, 125)).intents
        self.assertEqual(intent.side, "sell")

    def test_fade_mode_sells_into_the_same_flow(self):
        (intent,) = self.push_up(self.make(follow=False)).intents
        self.assertEqual(intent.side, "sell")

    def test_entry_price_stays_put_until_its_ttl(self):
        strategy = self.make(entry_ttl_seconds=6)
        first = self.push_up(strategy).intents[0]
        later = strategy.decide(tick(1005.0, 46520, 200))
        self.assertEqual(later.intents, (first,))

    def test_long_position_rests_a_target_and_stops_out_by_crossing(self):
        strategy = self.make()
        self.ledger.on_fill(Fill("buy", 46500, 1, "A1", 1000.0))
        (target,) = strategy.decide(tick(1001.0, 46505, 10)).intents
        self.assertEqual((target.side, target.role, target.price), ("sell", "exit", 46530))
        (stop,) = strategy.decide(tick(1002.0, 46478, 11)).intents
        self.assertEqual((stop.side, stop.role), ("sell", "risk_exit"))
        self.assertEqual(stop.price, 46477 - 2)
        held = strategy.decide(tick(1003.0, 46470, 12)).intents
        self.assertEqual(held, (stop,))

    def test_target_from_a_split_fill_average_lands_on_a_tick(self):
        strategy = self.make()
        self.ledger.on_fill(Fill("sell", 46500, 1, "A1", 1000.0))
        self.ledger.on_fill(Fill("sell", 46501, 1, "A2", 1000.0))
        (target,) = strategy.decide(tick(1001.0, 46495, 10)).intents
        self.assertEqual((target.side, target.price), ("buy", 46470))

    def test_max_hold_forces_an_exit(self):
        strategy = self.make(max_hold_seconds=60)
        self.ledger.on_fill(Fill("buy", 46500, 1, "A1", 1000.0))
        (intent,) = strategy.decide(tick(1061.0, 46505, 10)).intents
        self.assertEqual((intent.role, intent.reason), ("risk_exit", "max hold"))

    def test_daily_loss_halts_new_entries(self):
        strategy = self.make(previous_day_equity=2_000_000, daily_loss_fraction=0.001)
        self.ledger.on_fill(Fill("buy", 46500, 1, "A1", 1000.0))
        self.ledger.on_fill(Fill("sell", 46200, 1, "A2", 1001.0))
        decision = self.push_up(strategy, start=1100.0)
        self.assertTrue(decision.halted)
        self.assertEqual(decision.intents, ())

    def test_entries_are_not_blocked_by_a_cost_estimate(self):
        (intent,) = self.push_up(self.make(target_ticks=1)).intents
        self.assertEqual(intent.role, "entry")

if __name__ == "__main__":
    unittest.main()
