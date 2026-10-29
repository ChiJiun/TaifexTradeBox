import json
import unittest
from datetime import date
from pathlib import Path

import sim_hft_bot

from strategy_core import Fill, PositionLedger, Quote, StrategyConfig
from strategies.grid_pingpong import GridPingPongStrategy
from test_calendar import CAL
from ttb.calendar import CONTRACT_CODE, ContractInfo, TradingCalendar, front_month_contract
from ttb.contracts import point_value_for
from ttb.probe import far_limit_price


def tick(clock, last, t, symbol="CDFK6"):
    return Quote(symbol=symbol, bid=last - 1, ask=last + 1, bid_qty=5, ask_qty=5, last=last, last_qty=1,
                 total_volume=int(t), exchange_time=clock, received_at=t)


def by_side(decision):
    return {intent.side: intent for intent in decision.intents}


class GridPingPongTests(unittest.TestCase):
    def make(self, qty=1, **settings):
        config = StrategyConfig(symbol="CDFK6", tick_size=5, point_value=2000, fee_per_fill=50, tax_rate=0.00002,
                                base_qty=qty, max_order_qty=3 * qty, max_inventory=3 * qty)
        self.ledger = PositionLedger(config)
        settings.setdefault("grid_points", 5)
        settings.setdefault("levels", 3)
        settings.setdefault("stop_grids", 1)
        return GridPingPongStrategy(config, self.ledger, **settings)

    def fill(self, side, price, qty=1):
        self.ledger.on_fill(Fill(side, price, qty, f"F{price}", 0.0))

    def test_flat_grid_quotes_one_level_each_side(self):
        strategy = self.make()
        orders = by_side(strategy.decide(tick("09:00:00", 2431, 1.0)))
        self.assertEqual((orders["buy"].price, orders["buy"].role), (2425, "entry"))
        self.assertEqual((orders["sell"].price, orders["sell"].role), (2435, "entry"))

    def test_no_entries_before_the_first_entry_minute(self):
        strategy = self.make()
        self.assertEqual(strategy.decide(tick("08:45:30", 2431, 1.0)).intents, ())

    def test_long_level_exits_one_grid_higher_and_arms_the_next_level(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        self.fill("buy", 2425)
        orders = by_side(strategy.decide(tick("09:00:05", 2425, 2.0)))
        self.assertEqual((orders["sell"].price, orders["sell"].role), (2430, "exit"))
        self.assertEqual((orders["buy"].price, orders["buy"].role), (2420, "entry"))
        self.fill("buy", 2420)
        orders = by_side(strategy.decide(tick("09:00:10", 2420, 3.0)))
        self.assertEqual((orders["sell"].price, orders["buy"].price), (2425, 2415))

    def test_a_fill_reported_after_a_reanchor_still_exits_one_grid_higher(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        # The 2425 buy filled, but the ledger has not seen it yet: flat re-anchor.
        strategy.decide(tick("09:00:01", 2425, 2.0))
        self.fill("buy", 2425)
        orders = by_side(strategy.decide(tick("09:00:03", 2425, 3.0)))
        self.assertEqual((orders["sell"].price, orders["sell"].role), (2430, "exit"))
        self.assertEqual((orders["buy"].price, orders["buy"].role), (2420, "entry"))

    def test_a_gap_fill_reported_late_does_not_exit_below_the_entry(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        strategy.decide(tick("09:00:01", 2420, 2.0))
        self.fill("buy", 2425)
        orders = by_side(strategy.decide(tick("09:00:03", 2420, 3.0)))
        self.assertEqual(orders["sell"].price, 2430)

    def test_short_side_mirrors_the_long_side(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        self.fill("sell", 2435)
        orders = by_side(strategy.decide(tick("09:00:05", 2435, 2.0)))
        self.assertEqual((orders["buy"].price, orders["buy"].role), (2430, "exit"))
        self.assertEqual((orders["sell"].price, orders["sell"].role), (2440, "entry"))

    def test_full_side_only_exits(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        for price in (2425, 2420, 2415):
            self.fill("buy", price)
        (exit_order,) = strategy.decide(tick("09:01:00", 2414, 2.0)).intents
        self.assertEqual((exit_order.side, exit_order.price, exit_order.role), ("sell", 2420, "exit"))

    def test_stop_flattens_everything_even_after_a_partial_fill(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        for price in (2425, 2420, 2415):
            self.fill("buy", price)
        # Outermost level 2415 plus one stop grid: 2410.
        (stop,) = strategy.decide(tick("09:02:00", 2410, 2.0)).intents
        self.assertEqual((stop.side, stop.quantity, stop.role, stop.price), ("sell", 3, "risk_exit", 2409 - 10))
        self.fill("sell", 2399)
        # Two lots left: no level re-armed above the market, keep flattening.
        (rest,) = strategy.decide(tick("09:02:01", 2412, 3.0)).intents
        self.assertEqual((rest.side, rest.quantity, rest.role), ("sell", 2, "risk_exit"))
        self.fill("sell", 2399, 2)
        orders = by_side(strategy.decide(tick("09:02:05", 2401, 4.0)))
        self.assertEqual((orders["buy"].price, orders["sell"].price), (2395, 2405))

    def test_a_gap_through_the_stop_line_flattens_without_filling_every_level(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        self.fill("buy", 2425)
        # Three levels plus one stop grid under the 2430 anchor: 2410.
        (stop,) = strategy.decide(tick("09:00:05", 2405, 2.0)).intents
        self.assertEqual((stop.side, stop.quantity, stop.role), ("sell", 1, "risk_exit"))

    def test_a_fast_drop_never_buys_above_the_market(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        self.fill("buy", 2425)
        # The next level 2420 is already above the market: only the exit rests.
        (exit_order,) = strategy.decide(tick("09:00:05", 2416, 2.0)).intents
        self.assertEqual((exit_order.side, exit_order.price, exit_order.role), ("sell", 2430, "exit"))
        orders = by_side(strategy.decide(tick("09:00:10", 2421, 3.0)))
        self.assertEqual((orders["buy"].price, orders["buy"].role), (2420, "entry"))

    def test_stop_holds_its_price_until_the_ttl_expires(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        for price in (2425, 2420, 2415):
            self.fill("buy", price)
        (stop,) = strategy.decide(tick("09:02:00", 2410, 2.0)).intents
        self.assertEqual(strategy.decide(tick("09:02:02", 2405, 4.0)).intents, (stop,))
        (renewed,) = strategy.decide(tick("09:02:07", 2405, 9.0)).intents
        self.assertEqual(renewed.price, 2404 - 10)

    def test_flat_grid_follows_price_once_it_moves_a_grid(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        orders = by_side(strategy.decide(tick("09:00:01", 2441, 2.0)))
        self.assertEqual((orders["buy"].price, orders["sell"].price), (2435, 2445))

    def test_late_session_only_exits_then_flattens(self):
        strategy = self.make()
        strategy.decide(tick("09:00:00", 2430, 1.0))
        self.fill("buy", 2425)
        (exit_order,) = strategy.decide(tick("13:21:00", 2426, 2.0)).intents
        self.assertEqual((exit_order.side, exit_order.role), ("sell", "exit"))
        (flatten,) = strategy.decide(tick("13:35:00", 2426, 3.0)).intents
        self.assertEqual((flatten.side, flatten.quantity, flatten.role), ("sell", 1, "risk_exit"))
        self.fill("sell", 2425)
        self.assertEqual(strategy.decide(tick("13:35:02", 2426, 4.0)).intents, ())

    def test_restart_with_inventory_recovers_the_anchor(self):
        self.make()
        self.fill("buy", 2425)
        self.fill("buy", 2420)
        strategy = GridPingPongStrategy(self.ledger.config, self.ledger, grid_points=5, levels=3, stop_grids=1)
        orders = by_side(strategy.decide(tick("10:00:00", 2421, 1.0)))
        self.assertEqual(strategy.anchor, 2430)
        self.assertEqual((orders["sell"].price, orders["buy"].price), (2425, 2415))

    def test_partial_level_exits_only_what_it_holds(self):
        strategy = self.make(qty=5)
        strategy.decide(tick("09:00:00", 2430, 1.0))
        self.fill("buy", 2425, 7)
        orders = by_side(strategy.decide(tick("09:00:05", 2421, 2.0)))
        self.assertEqual((orders["sell"].price, orders["sell"].quantity), (2425, 2))
        self.assertEqual((orders["buy"].price, orders["buy"].quantity), (2415, 5))

    def test_grid_must_fit_the_tick(self):
        with self.assertRaises(ValueError):
            self.make(grid_points=4)
        with self.assertRaises(ValueError):
            self.make(levels=4)
        with self.assertRaises(ValueError):
            self.make(reanchor_grids=2)


class GridTemplateTests(unittest.TestCase):
    def test_grid_templates_build_the_strategy_and_a_probe_inside_the_price_limit(self):
        folder = Path(__file__).resolve().parent / "automation"
        for name, symbol, last in (("ccf", "CCFK6", 146.0), ("cdf", "CDFK6", 2431.0)):
            with self.subTest(template=name):
                config = json.loads((folder / f"config.grid-{name}.example.json").read_text(encoding="utf-8"))
                args = sim_hft_bot.parse_args(["--symbol", symbol, "--strategy-module", config["strategyModule"]]
                                              + config["extraArgs"])
                strategy_config = sim_hft_bot.build_config(args)
                strategy = sim_hft_bot.build_strategy(args, strategy_config, PositionLedger(strategy_config))
                self.assertIsInstance(strategy, GridPingPongStrategy)
                # A stop flattens every level in one order.
                self.assertEqual(strategy_config.max_order_qty, strategy.settings.levels)
                self.assertEqual(config["autoRecovery"]["maxFlattenQty"], strategy.settings.levels)
                gate = config["liveGate"]
                quote = Quote(symbol=symbol, bid=last, ask=last + gate["tickSize"], bid_qty=5, ask_qty=5, last=last,
                              last_qty=1, total_volume=1, exchange_time="09:00:00", received_at=1.0)
                price = far_limit_price(quote, "buy", gate["tickSize"], gate["distanceTicks"])
                # Far enough not to fill, inside the 10% daily limit.
                self.assertTrue(last * 0.9 < price < last * 0.96, price)


class StockFuturesContractTests(unittest.TestCase):
    def test_stock_futures_resolve_like_index_futures(self):
        contract = ContractInfo.from_symbol("CDFK6", 2026)
        self.assertEqual((contract.product, contract.month, contract.third_wednesday), ("CDF", 11, date(2026, 11, 18)))
        calendar = TradingCalendar.from_file(CAL)
        self.assertEqual(front_month_contract("CCF", date(2026, 10, 29), calendar).symbol, "CCFK6")

    def test_unlisted_futures_stay_unsupported(self):
        for code in ("GDFK6", "QFFK6", "CDOK6", "PLFK6"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                ContractInfo.from_symbol(code, 2026)
        self.assertIsNotNone(CONTRACT_CODE.fullmatch("CDFK6"))
        self.assertIsNone(CONTRACT_CODE.fullmatch("GDFK6"))

    def test_stock_futures_point_value_is_two_thousand(self):
        self.assertEqual(point_value_for("CCFK6"), 2000.0)
        with self.assertRaises(ValueError):
            point_value_for("CDFK6", 200.0)


if __name__ == "__main__":
    unittest.main()
