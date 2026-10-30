import json
import tempfile
import unittest
from pathlib import Path

from strategy_core import OrderIntent, PositionLedger, Quote, StrategyConfig
from ttb.orders import TTBOrderGateway
from ttb.risk import (
    DurableOrderRateGuard,
    PreTradeLimits,
    PreTradeRiskError,
    parse_available_margin,
    validate_exposure,
)


class MarginParserTests(unittest.TestCase):
    def test_verified_field_from_nested_data(self):
        self.assertEqual(
            parse_available_margin(
                {"Code": "0000", "Data": '[{"Available":"12,345.50"}]'},
                "Available",
            ), 12345.5,
        )

    def test_missing_field_duplicate_field_and_bad_values_fail_closed(self):
        for response in (
            {"Code": "0000", "Data": [{"Margin": 3000}]},
            {"Code": "0000", "Data": [{"Available": "1"}, {"Available": "2"}]},
            {"Code": "0000", "Data": [{"Available": "NaN"}]},
            {"Code": "9999", "Data": [{"Available": 100}]},
        ):
            with self.subTest(response=response):
                with self.assertRaises(PreTradeRiskError):
                    parse_available_margin(response, "Available")

    def test_margin_limits_require_explicit_field_and_positive_headroom(self):
        with self.assertRaises(ValueError):
            PreTradeLimits(available_margin_field="Available").validate()
        with self.assertRaises(ValueError):
            PreTradeLimits(minimum_available_margin=100).validate()


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(
            symbol="TXFA7", max_inventory=1, max_order_qty=1, point_value=200,
        )
        self.limits = PreTradeLimits(max_notional=5_000_000)

    def test_both_sided_market_making_is_bounded_by_worst_case_position(self):
        validate_exposure(
            OrderIntent("sell", 20001, 1, "entry", "test"),
            0, {"buy": {"qty": 1}}, self.config, self.limits,
        )
        with self.assertRaisesRegex(PreTradeRiskError, "max_inventory"):
            validate_exposure(
                OrderIntent("buy", 20000, 1, "entry", "test"),
                0, {"buy": {"qty": 1}}, self.config, self.limits,
            )

    def test_reduction_only_exit_is_allowed_not_reversal(self):
        validate_exposure(
            OrderIntent("sell", 20000, 1, "risk_exit", "stop"),
            1, {}, self.config, self.limits,
        )
        with self.assertRaisesRegex(PreTradeRiskError, "reduce-only"):
            validate_exposure(
                OrderIntent("buy", 20000, 1, "risk_exit", "not reduce"),
                1, {}, self.config, self.limits,
            )

    def test_notional_and_missing_limit_block_entry(self):
        for limits in (PreTradeLimits(), PreTradeLimits(max_notional=1_000_000)):
            with self.assertRaises(PreTradeRiskError):
                validate_exposure(
                    OrderIntent("buy", 20000, 1, "entry", "test"),
                    0, {}, self.config, limits,
                )


class RateGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.json"
        self.limits = PreTradeLimits(
            max_orders_per_minute=2,
            max_cancels_per_minute=1,
            max_daily_orders=2,
        )

    def test_durable_attempts_persist_across_restarts(self):
        first = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        first.record("order", 1_700_000_000.0)
        second = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        self.assertEqual(second.snapshot(1_700_000_010.0)["orders_last_minute"], 1)
        second.record("order", 1_700_000_011.0)
        third = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        with self.assertRaisesRegex(PreTradeRiskError, "per minute"):
            third.assert_entry_budget(1_700_000_012.0)

    def test_excessive_cancels_block_entries_but_not_cancellations(self):
        rate = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        rate.record("cancel", 1_700_000_000.0)
        with self.assertRaisesRegex(PreTradeRiskError, "max cancels"):
            rate.assert_entry_budget(1_700_000_001.0)
        rate.record("cancel", 1_700_000_002.0)

    def test_daily_limit_remains_after_minute_window(self):
        rate = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        rate.record("order", 1_700_000_000.0)
        rate.record("order", 1_700_000_070.0)
        with self.assertRaisesRegex(PreTradeRiskError, "daily"):
            rate.assert_entry_budget(1_700_000_080.0)

    def test_v1_counter_file_is_migrated_without_resetting_daily_order_count(self):
        old = {
            "version": 1, "symbol": "TXFA7",
            "events": [
                {"kind": "order", "timestamp": 1_700_000_000.0},
                {"kind": "order", "timestamp": 1_700_000_080.0},
                {"kind": "cancel", "timestamp": 1_700_000_090.0},
            ],
        }
        self.path.write_text(json.dumps(old), encoding="utf-8")
        rate = DurableOrderRateGuard(
            self.path, "TXFA7", PreTradeLimits(max_cancels_per_minute=10, max_daily_orders=2)
        )
        self.assertEqual(rate.snapshot(1_700_000_091.0)["orders_today_taipei"], 2)
        with self.assertRaisesRegex(PreTradeRiskError, "daily"):
            rate.assert_entry_budget(1_700_000_091.0)
        rate.record("cancel", 1_700_000_092.0)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["daily_orders"], 2)

    def test_counter_file_keeps_only_recent_events(self):
        rate = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        for i in range(120):
            rate.record("cancel", 1_700_000_000.0 + i)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 2)
        self.assertLessEqual(len(stored["events"]), 60)
        self.assertEqual(stored["daily_orders"], 0)
        self.assertEqual(rate.snapshot(1_700_000_119.0)["cancels_last_minute"], 60)

    def test_clock_rollback_and_corrupt_state_fail_closed(self):
        rate = DurableOrderRateGuard(self.path, "TXFA7", self.limits)
        rate.record("order", 1_700_000_100.0)
        with self.assertRaisesRegex(PreTradeRiskError, "clock moved backward"):
            rate.assert_entry_budget(1_700_000_000.0)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(PreTradeRiskError, "could not be restored"):
            DurableOrderRateGuard(self.path, "TXFA7", self.limits)


def quote():
    return Quote(
        symbol="TXFA7", bid=20000, ask=20001,
        bid_qty=1, ask_qty=1, last=20000,
        last_qty=1, total_volume=1, exchange_time="09:00:00",
        received_at=100.0,
    )


class RiskGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "risk.json"
        self.config = StrategyConfig(symbol="TXFA7", max_inventory=1, max_order_qty=1)

    def gateway(self, margin="10000000", limits=None):
        class FakeModule:
            def __init__(self):
                self.orders = 0
                self.margin_queries = 0

            def QUERYMARGIN(self):
                self.margin_queries += 1
                return {"Code": "0000", "Data": [{"Available": margin}]}

            def QUERYRESTOREREPORT(self):
                return {"Code": "0000", "Data": []}

            def NEWORDER(self, payload):
                self.orders += 1
                return {"Code": "0000", "OrdNo": "A1"}

        module = FakeModule()
        gw = TTBOrderGateway(
            module, self.config, PositionLedger(self.config), 0.25,
            risk_limits=limits or PreTradeLimits(
                max_notional=5_000_000, minimum_available_margin=1000,
                available_margin_field="Available",
            ),
            risk_counters_path=self.path,
        )
        gw.bootstrapped = True
        return module, gw

    def test_broker_margin_below_headroom_skips_the_entry_without_stopping(self):
        module, gateway = self.gateway(margin="100")
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.assertEqual(module.orders, 0)
        self.assertIn("below required", gateway.risk_block_reason)
        # Retried only after the back-off, so a full account is not polled every tick.
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.assertEqual(module.margin_queries, 1)

    def test_a_blocked_exit_still_stops_the_bot(self):
        module, gateway = self.gateway(margin="100")
        with self.assertRaisesRegex(PreTradeRiskError, "reduce-only"):
            gateway.sync((OrderIntent("sell", 20000, 1, "exit", "test"),), quote())
        self.assertEqual(module.orders, 0)

    def test_missing_margin_configuration_fails_closed(self):
        module, gateway = self.gateway(limits=PreTradeLimits(max_notional=5_000_000))
        with self.assertRaisesRegex(PreTradeRiskError, "verified"):
            gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.assertEqual(module.orders, 0)

    def test_health_snapshot_survives_counter_clock_failure(self):
        _module, gateway = self.gateway()
        gateway.order_rate.record("cancel", __import__("time").time() + 3600)
        result = gateway.snapshot()
        self.assertIn("error", result["rate_counters"])

    def test_gateway_blocks_oversized_intent_before_broker_api(self):
        module, gateway = self.gateway(
            limits=PreTradeLimits(
                max_notional=100_000_000, minimum_available_margin=1000,
                available_margin_field="Available",
            )
        )
        # max_order_qty=1 even if aggregate inventory is increased later.
        gateway.config.max_inventory = 5
        with self.assertRaisesRegex(PreTradeRiskError, "max_order_qty"):
            gateway.sync((OrderIntent("buy", 20000, 5, "entry", "custom strategy"),), quote())
        self.assertEqual(module.orders, 0)

    def test_valid_entry_consumes_durable_rate_budget(self):
        module, gateway = self.gateway()
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.assertEqual(module.orders, 1)
        self.assertEqual(gateway.order_rate.snapshot(__import__("time").time())["orders_last_minute"], 1)


if __name__ == "__main__":
    unittest.main()
