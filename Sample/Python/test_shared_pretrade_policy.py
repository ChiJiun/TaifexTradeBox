import tempfile
import time
import unittest
from pathlib import Path

from strategy_core import OrderIntent, PositionLedger, Quote, StrategyConfig
from ttb.orders import TTBOrderGateway
from ttb.risk import (
    DurableOrderRateGuard,
    PreTradeLimits,
    PreTradeRiskError,
    assess_pretrade,
)


def quote():
    return Quote(
        symbol="TXFA7", bid=20000, ask=20001, bid_qty=10, ask_qty=10,
        last=20000, last_qty=1, total_volume=100, exchange_time="09:00:00",
        received_at=time.monotonic(),
    )


class SharedRiskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config = StrategyConfig(
            symbol="TXFA7", max_inventory=1, max_order_qty=1,
            point_value=200,
        )
        self.limits = PreTradeLimits(
            max_notional=10_000_000, minimum_available_margin=5000,
            available_margin_field="Available", max_orders_per_minute=1,
        )
        self.rate = DurableOrderRateGuard(self.path / "rates.json", "TXFA7", self.limits)
        self.intent = OrderIntent("buy", 20000, 1, "entry", "test")

    def assess(self, *, limits=None, position=0, active=None, margin_response=None, intent=None):
        return assess_pretrade(
            intent or self.intent, position, active or {}, self.config,
            limits or self.limits, order_rate=self.rate,
            now=time.time(),
            margin_supplier=lambda: margin_response if margin_response is not None else
            {"Code": "0000", "Data": [{"Available": "25000"}]},
        )

    def test_synthetic_margin_fixture_passes_without_ttb_module(self):
        self.assess()
        self.assertFalse((self.path / "ttb-network.log").exists())

    def test_low_margin_and_missing_field_block(self):
        for response in (
            {"Code": "0000", "Data": [{"Available": "10"}]},
            {"Code": "0000", "Data": [{"Different": 999999}]},
            {"Code": "9999", "Data": [{"Available": 999999}]},
        ):
            with self.subTest(response=response):
                with self.assertRaises(PreTradeRiskError):
                    self.assess(margin_response=response)

    def test_notional_and_inventory_and_quantity_ceiling_block(self):
        with self.assertRaisesRegex(PreTradeRiskError, "max notional"):
            self.assess(limits=PreTradeLimits(
                max_notional=1000, minimum_available_margin=5000,
                available_margin_field="Available",
            ))
        with self.assertRaisesRegex(PreTradeRiskError, "max_inventory"):
            self.assess(position=1)
        with self.assertRaisesRegex(PreTradeRiskError, "max_order_qty"):
            self.assess(intent=OrderIntent("buy", 20000, 2, "entry", "too many"))

    def test_entry_budget_persists_for_subsequent_offline_scenario(self):
        self.assess()
        self.rate.record("order", time.time())
        with self.assertRaisesRegex(PreTradeRiskError, "per minute"):
            self.assess()

    def test_reduce_only_exit_skips_margin_provider(self):
        def unexpected():
            raise AssertionError("margin provider must not be called for reduce-only exit")
        assess_pretrade(
            OrderIntent("sell", 20000, 1, "risk_exit", "stop"),
            1, {}, self.config, self.limits,
            order_rate=self.rate, now=time.time(), margin_supplier=unexpected,
        )

    def test_gateway_risk_block_is_visible_and_audited_before_no_order(self):
        class FakeModule:
            def __init__(self):
                self.orders = 0
            def QUERYMARGIN(self):
                return {"Code": "0000", "Data": [{"Available": "10"}]}
            def QUERYRESTOREREPORT(self):
                return {"Code": "0000", "Data": []}

            def NEWORDER(self, payload):
                self.orders += 1
                return {"Code": "0000", "OrdNo": "A1"}

        fake = FakeModule()
        gateway = TTBOrderGateway(
            fake, self.config, PositionLedger(self.config), 0.25,
            risk_limits=self.limits,
            risk_counters_path=self.path / "gateway-rate.json",
            journal_dir=self.path / "journal",
        )
        gateway.bootstrapped = True
        # A margin block only skips the entry; the bot keeps running.
        gateway.sync((self.intent,), quote())
        self.assertEqual(fake.orders, 0)
        self.assertIn("headroom", gateway.snapshot()["risk_block_reason"])
        self.assertEqual(
            [r["kind"] for r in gateway.journal.events(gateway.journal.local_day())],
            ["risk_block"],
        )


if __name__ == "__main__":
    unittest.main()
