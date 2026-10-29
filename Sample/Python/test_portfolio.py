import json
import tempfile
import unittest
from pathlib import Path

from strategy_core import Fill, FeatureEngine, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision
from strategies.portfolio import KellyStats, PortfolioStrategy


class Scripted:
    """Member that works the test's orders until its position changes, as real members do."""

    def __init__(self, config, ledger, **params):
        self.config = config
        self.ledger = ledger
        self.features = FeatureEngine(config)
        self.intents = ()
        self.position = 0

    def want(self, *intents):
        self.intents = intents
        self.position = self.ledger.position

    def decide(self, quote):
        if self.ledger.position != self.position:
            self.intents = ()
        return StrategyDecision(self.intents, self.features.update(quote), False, "scripted")


def tick(clock, last, t, volume):
    return Quote(symbol="TXFK6", bid=last - 1, ask=last + 1, bid_qty=5, ask_qty=5, last=last, last_qty=1,
                 total_volume=volume, exchange_time=clock, received_at=t)


def member(name, mean=2000.0, sd=9000.0, max_qty=2):
    return {"name": name, "module": f"{__name__}:Scripted", "max_qty": max_qty,
            "prior": {"n": 30, "mean": mean, "sd": sd}}


class PortfolioTests(unittest.TestCase):
    def make(self, *members, **settings):
        config = StrategyConfig(symbol="TXFK6", point_value=200, fee_per_fill=50, tax_rate=0.00002,
                                base_qty=1, max_order_qty=2, max_inventory=2)
        self.ledger = PositionLedger(config)
        settings.setdefault("latency_seconds", 1.0)
        strategy = PortfolioStrategy(config, self.ledger, members=list(members or (member("a"), member("b"))),
                                     **settings)
        return strategy, strategy.books

    def test_member_fills_only_after_latency_and_a_print_through_its_price(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("buy", 46490, 1, "entry", "dip"))
        strategy.decide(tick("10:00:00", 46490, 1.5, 101))  # placed; not live yet
        self.assertEqual(a.ledger.position, 0)
        strategy.decide(tick("10:00:01", 46495, 3.0, 102))  # live, but the print is above the price
        self.assertEqual(a.ledger.position, 0)
        strategy.decide(tick("10:00:02", 46488, 4.0, 102))  # no volume traded
        self.assertEqual(a.ledger.position, 0)
        strategy.decide(tick("10:00:03", 46488, 5.0, 103))
        self.assertEqual((a.ledger.position, a.ledger.average_price), (1, 46488))

    def test_fill_quantity_is_capped_by_traded_volume(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("buy", 46510, 2, "entry", "burst"))
        strategy.decide(tick("10:00:00", 46500, 1.5, 100))
        strategy.decide(tick("10:00:03", 46500, 3.0, 101))
        self.assertEqual(a.ledger.position, 1)

    def test_opposite_members_net_to_no_real_order(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("buy", 46510, 1, "entry", "long"))
        b.strategy.want(OrderIntent("sell", 46490, 1, "entry", "short"))
        strategy.decide(tick("10:00:00", 46500, 1.5, 100))
        decision = strategy.decide(tick("10:00:03", 46500, 3.0, 110))
        self.assertEqual((a.ledger.position, b.ledger.position), (1, -1))
        self.assertEqual(decision.intents, ())

    def test_real_account_follows_the_net_with_a_marketable_order(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("buy", 46510, 1, "entry", "long"))
        strategy.decide(tick("10:00:00", 46500, 1.5, 100))
        (intent,) = strategy.decide(tick("10:00:03", 46500, 3.0, 110)).intents
        self.assertEqual((intent.side, intent.price, intent.quantity, intent.role), ("buy", 46501 + 2, 1, "entry"))
        # The order keeps its price until the TTL so it can reach TTB.
        self.assertEqual(strategy.decide(tick("10:00:04", 46520, 4.0, 111)).intents, (intent,))
        (requote,) = strategy.decide(tick("10:00:10", 46520, 10.0, 112)).intents
        self.assertEqual(requote.price, 46521 + 2)
        self.ledger.on_fill(Fill("buy", 46521, 1, "R1", 10.5))
        self.assertEqual(strategy.decide(tick("10:00:11", 46520, 11.0, 113)).intents, ())

    def test_marketable_member_order_reaches_the_account_without_waiting_for_its_fill(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("buy", 46503, 1, "entry", "burst"))
        (intent,) = strategy.decide(tick("10:00:00", 46500, 1.5, 100)).intents
        self.assertEqual((a.ledger.position, intent.side, intent.quantity), (0, "buy", 1))
        a.strategy.want(OrderIntent("buy", 46490, 1, "entry", "passive"))
        self.assertEqual(strategy.decide(tick("10:00:01", 46500, 2.0, 100)).intents, ())

    def test_resting_member_exit_is_mirrored_at_its_price(self):
        strategy, (a, b) = self.make()
        self.ledger.on_fill(Fill("buy", 46500, 1, "R1", 0.5))
        a.ledger.on_fill(Fill("buy", 46500, 1, "T1", 0.5))
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("sell", 46600, 1, "exit", "target"))
        (mirror,) = strategy.decide(tick("10:00:01", 46500, 2.0, 101)).intents
        self.assertEqual((mirror.side, mirror.price, mirror.quantity, mirror.role), ("sell", 46600, 1, "exit"))
        # The theory fills on the touch; the account keeps its order while the fill report arrives.
        self.assertEqual(strategy.decide(tick("10:00:05", 46600, 6.0, 102)).intents, (mirror,))
        self.assertEqual(a.ledger.position, 0)
        (chase,) = strategy.decide(tick("10:00:12", 46590, 13.0, 103)).intents
        self.assertEqual((chase.side, chase.price), ("sell", 46589 - 2))

    def test_member_stop_does_not_wait_on_the_mirrored_target(self):
        strategy, (a, b) = self.make()
        self.ledger.on_fill(Fill("buy", 46500, 1, "R1", 0.5))
        a.ledger.on_fill(Fill("buy", 46500, 1, "T1", 0.5))
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("sell", 46600, 1, "exit", "target"))
        strategy.decide(tick("10:00:01", 46500, 2.0, 101))
        a.strategy.want(OrderIntent("sell", 46437, 1, "risk_exit", "stop"))
        (stop,) = strategy.decide(tick("10:00:02", 46440, 3.0, 102)).intents
        self.assertEqual((stop.side, stop.price, stop.role), ("sell", 46439 - 2, "exit"))

    def test_flip_reduces_to_flat_before_reversing(self):
        strategy, (a, b) = self.make()
        self.ledger.on_fill(Fill("buy", 46500, 1, "R1", 0.5))
        a.ledger.on_fill(Fill("buy", 46500, 1, "T1", 0.5))
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("sell", 46490, 2, "exit", "reverse"))
        strategy.decide(tick("10:00:00", 46500, 1.5, 100))
        (intent,) = strategy.decide(tick("10:00:03", 46500, 3.0, 110)).intents
        self.assertEqual(a.ledger.position, -1)
        self.assertEqual((intent.side, intent.quantity, intent.role), ("sell", 1, "exit"))
        self.ledger.on_fill(Fill("sell", 46499, 1, "R2", 3.5))
        (entry,) = strategy.decide(tick("10:00:04", 46500, 4.0, 111)).intents
        self.assertEqual((entry.side, entry.quantity, entry.role), ("sell", 1, "entry"))

    def test_kelly_sizes_by_edge_and_stops_a_losing_member(self):
        self.assertEqual(KellyStats(30, 2000, 9000).contracts(2_000_000, 0.5, 5), 5)
        self.assertEqual(KellyStats(30, 200, 9000).contracts(2_000_000, 0.5, 5), 2)
        stats = KellyStats(30, 1000, 9000)
        for _ in range(40):
            stats.add(-2000)
        self.assertEqual(stats.contracts(2_000_000, 0.5, 5), 0)

    def test_member_without_edge_trades_theory_but_not_the_account(self):
        strategy, (a, b) = self.make(member("a"), member("b", mean=-100))
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        self.assertEqual((a.qty, b.qty), (2, 0))
        b.strategy.want(OrderIntent("buy", 46510, 1, "entry", "shadow"))
        strategy.decide(tick("10:00:00", 46500, 1.5, 100))
        decision = strategy.decide(tick("10:00:03", 46500, 3.0, 110))
        self.assertEqual(b.ledger.position, 1)
        self.assertEqual(decision.intents, ())

    def test_no_member_entries_in_the_opening_delay(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("08:50:00", 46500, 1.0, 100))
        a.strategy.want(OrderIntent("buy", 46510, 1, "entry", "early"))
        strategy.decide(tick("08:50:00", 46500, 1.5, 100))
        strategy.decide(tick("08:50:03", 46500, 3.0, 110))
        self.assertEqual(a.ledger.position, 0)

    def test_close_flattens_theory_and_records_the_trade(self):
        with tempfile.TemporaryDirectory() as folder:
            strategy, (a, b) = self.make(journal_dir=folder)
            strategy.decide(tick("13:00:00", 46500, 1.0, 100))
            a.strategy.want(OrderIntent("buy", 46510, 1, "entry", "long"))
            strategy.decide(tick("13:00:00", 46500, 1.5, 100))
            strategy.decide(tick("13:00:03", 46500, 3.0, 110))
            self.assertEqual(a.ledger.position, 1)
            decision = strategy.decide(tick("13:35:00", 46600, 4.0, 120))
            self.assertEqual(a.ledger.position, 0)
            self.assertEqual(a.stats.n, 1)
            self.assertEqual(decision.intents, ())
            journal = next(Path(folder).glob("theory-*.jsonl")).read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in journal]
            closed = [e for e in events if e["event"] == "trade_closed"]
            self.assertEqual(closed[0]["member"], "a")
            self.assertAlmostEqual(closed[0]["pnl"], 99 * 200 - 100 - 0.00002 * 200 * (46500 + 46599), places=0)
            self.assertIn("order", {e["event"] for e in events})
            self.assertIn("sizing", {e["event"] for e in events})
            # A restarted bot keeps the live record.
            again, (a2, _) = self.make(journal_dir=folder)
            self.assertEqual(a2.stats.n, 1)

    def test_member_strategy_trades_its_kelly_size(self):
        strategy, (a, b) = self.make()
        strategy.decide(tick("10:00:00", 46500, 1.0, 100))
        self.assertEqual(a.strategy.config.base_qty, 2)

    def test_competition_template_builds_the_portfolio(self):
        template = Path(__file__).resolve().parent / "automation" / "config.competition.example.json"
        config = json.loads(template.read_text(encoding="utf-8"))
        args = config["extraArgs"]
        params = json.loads(args[args.index("--strategy-params-json") + 1])
        self.assertEqual(config["strategyModule"], "strategies.portfolio:PortfolioStrategy")
        self.assertLessEqual(len(args[args.index("--strategy-params-json") + 1]), 4096)
        members = params.pop("members")
        strategy, books = self.make(*members, **params)
        self.assertEqual([book.name for book in books], ["burst", "fade"])
        self.assertEqual(strategy.config.symbol, "TXFK6")


if __name__ == "__main__":
    unittest.main()
