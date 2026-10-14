from __future__ import annotations

import json
import unittest

from monte_carlo_market_maker import synthetic_quotes
from replay_market_maker import replay
from sim_hft_bot import build_config, build_strategy, parse_args
from strategy_core import PositionLedger, Quote, StrategyConfig
from strategies import CompetitionPnlShaping, OrderFlowGatedMaker, StickyOneTickMaker
from ttb.risk import PreTradeLimits, PreTradeRiskError, validate_exposure


def quote(t: float, *, bid_qty: int = 5, ask_qty: int = 5, last: float = 100.5) -> Quote:
    return Quote(
        symbol="TXFA7", bid=100, ask=101, bid_qty=bid_qty, ask_qty=ask_qty,
        last=last, last_qty=1, total_volume=int(t) + 1,
        exchange_time="09:00:00", received_at=t,
    )


def cfg(**kwargs) -> StrategyConfig:
    options = dict(symbol="TXFA7", tick_size=1, point_value=200,
                   fee_per_fill=0, tax_rate=0, warmup_ticks=2,
                   max_inventory=1, max_order_qty=1,
                   target_ticks=2, stop_ticks=4,
                   virtual_max_mode=True)
    options.update(kwargs)
    return StrategyConfig(**options)


class ExecutableStrategyModuleTests(unittest.TestCase):
    def test_modules_build_through_official_strategy_loader_and_json_params(self):
        for spec, cls, params in (
            ("strategies.sticky_maker:StickyOneTickMaker", StickyOneTickMaker,
             {"require_positive_one_tick_edge": True}),
            ("strategies.order_flow_maker:OrderFlowGatedMaker", OrderFlowGatedMaker,
             {"entry_threshold": 0.25, "short_window": 2, "long_window": 3}),
            ("strategies.competition_pnl:CompetitionPnlShaping", CompetitionPnlShaping,
             {"previous_day_equity": 200000, "daily_profit_fraction": 0.001}),
        ):
            with self.subTest(spec=spec):
                args = parse_args(["--symbol", "TXFA7", "--strategy-module", spec,
                                   "--strategy-params-json", json.dumps(params)])
                config = build_config(args)
                strat = build_strategy(args, config, PositionLedger(config))
                self.assertIsInstance(strat, cls)
                self.assertEqual(strat.parameters().items() >= params.items(), True)
                self.assertEqual(json.loads(json.dumps(strat.parameters())), strat.parameters())

    def test_entry_price_stays_sticky_before_reprice_threshold(self):
        config = cfg(quote_ttl_seconds=10, reprice_threshold_ticks=10)
        strategy = StickyOneTickMaker(config, PositionLedger(config))
        first = strategy.decide(quote(1))
        initial = strategy.decide(quote(2))
        self.assertEqual(len(initial.intents), 2)
        later = strategy.decide(quote(3))
        self.assertEqual(initial.intents, later.intents)
        self.assertEqual(len(first.intents), 0)

    def test_one_tick_cycle_without_net_cost_is_blocked(self):
        config = cfg(target_ticks=1, fee_per_fill=110)
        strat = StickyOneTickMaker(config, PositionLedger(config))
        strat.decide(quote(1))
        decision = strat.decide(quote(2))
        self.assertFalse(any(x.role == "entry" for x in decision.intents))

    def test_flow_gate_keeps_only_buy_in_positive_flow(self):
        config = cfg()
        strat = OrderFlowGatedMaker(config, PositionLedger(config),
                                   entry_threshold=0.15, short_window=2, long_window=3)
        strat.decide(quote(1, bid_qty=9, ask_qty=1, last=101))
        strat.decide(quote(2, bid_qty=9, ask_qty=1, last=101))
        result = strat.decide(quote(3, bid_qty=9, ask_qty=1, last=101))
        self.assertEqual({i.side for i in result.intents if i.role == "entry"}, {"buy"})
        self.assertIn("flow-gated", result.reason)

    def test_flow_gate_never_discards_reduce_only_risk_exit(self):
        config = cfg()
        ledger = PositionLedger(config)
        ledger.position = 1
        ledger.average_price = 105
        ledger.opened_at = 1
        strat = OrderFlowGatedMaker(config, ledger, entry_threshold=0.2,
                                   short_window=2, long_window=3)
        decision = strat.decide(quote(30, bid_qty=1, ask_qty=9, last=100))
        self.assertEqual(len(decision.intents), 1)
        self.assertEqual(decision.intents[0].side, "sell")
        self.assertEqual(decision.intents[0].role, "risk_exit")

    def test_pnl_shaping_rejects_unverified_equity_and_latches_profit(self):
        config = cfg()
        ledger = PositionLedger(config)
        no_equity = CompetitionPnlShaping(config, ledger)
        self.assertEqual(no_equity.decide(quote(1)).intents, ())
        self.assertEqual(no_equity.halt_reason, "previous_day_equity_unverified")

        ledger2 = PositionLedger(config)
        strat = CompetitionPnlShaping(config, ledger2, previous_day_equity=1000,
                                     daily_profit_fraction=0.1)
        ledger2.realized_pnl = 150
        decision = strat.decide(quote(5))
        self.assertTrue(decision.halted)
        self.assertEqual(strat.halt_reason, "daily_profit_target")
        ledger2.realized_pnl = 0
        self.assertEqual(strat.decide(quote(6)).intents, ())
        self.assertEqual(strat.halt_reason, "daily_profit_target")

    def test_daily_loss_checks_mark_to_market_for_existing_inventory(self):
        config = cfg()
        ledger = PositionLedger(config)
        ledger.position = 1
        ledger.average_price = 110
        ledger.opened_at = 1
        strat = CompetitionPnlShaping(config, ledger, previous_day_equity=1000,
                                     daily_loss_fraction=0.05)
        decision = strat.decide(quote(5))
        self.assertEqual(strat.halt_reason, "daily_equity_loss_limit")
        self.assertEqual(len(decision.intents), 1)
        self.assertEqual(decision.intents[0].role, "risk_exit")
        self.assertEqual(decision.intents[0].side, "sell")

    def test_deterministic_replay_all_three_executable_modules(self):
        rows = synthetic_quotes(90, "TXFA7", seed=19)
        for maker in (StickyOneTickMaker, OrderFlowGatedMaker, CompetitionPnlShaping):
            with self.subTest(strategy=maker.__name__):
                def create(config, ledger):
                    if maker is CompetitionPnlShaping:
                        return maker(config, ledger, previous_day_equity=2000000)
                    if maker is OrderFlowGatedMaker:
                        return maker(config, ledger, short_window=4, long_window=12)
                    return maker(config, ledger)
                a = replay(rows, cfg(), fill_model="independent",
                           strategy_factory=create, seed=17)
                b = replay(rows, cfg(), fill_model="independent",
                           strategy_factory=create, seed=17)
                self.assertEqual(a["marked_equity"], b["marked_equity"])
                self.assertEqual(a["filled_contracts"], b["filled_contracts"])
                self.assertLessEqual(abs(a["open_position"]), 1)

    def test_strategy_can_never_override_gateway_inventory_budget(self):
        config = cfg(max_order_qty=1)
        strat = StickyOneTickMaker(config, PositionLedger(config))
        strat.decide(quote(1))
        intent = strat.decide(quote(2)).intents[0]
        with self.assertRaises(PreTradeRiskError):
            validate_exposure(intent, 1, {}, config,
                              PreTradeLimits(max_notional=100000))
        with self.assertRaises(ValueError):
            parse = parse_args(["--symbol", "TXFA7", "--strategy-module",
                                "strategies.sticky_maker:StickyOneTickMaker",
                                "--strategy-params-json", "[1,2]"])
            build_strategy(parse, cfg(), PositionLedger(cfg()))


if __name__ == "__main__":
    unittest.main()
