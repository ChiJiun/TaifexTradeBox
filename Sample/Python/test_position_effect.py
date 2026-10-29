import tempfile
import unittest
from pathlib import Path

from strategy_core import Fill, OrderIntent, PositionLedger, Quote, StrategyConfig
from ttb.api import TTBApi, TTBOrderRequest, position_effect_for
from ttb.orders import TTBOrderGateway
from ttb.risk import PreTradeLimits


def quote() -> Quote:
    return Quote(
        symbol="TXFA7", bid=20000, ask=20001,
        bid_qty=1, ask_qty=1, last=20000,
        last_qty=1, total_volume=1, exchange_time="09:00:00",
        received_at=100.0,
    )


class FakeModule:
    def __init__(self):
        self.payloads = []
        self.cancels = []

    def QUERYMARGIN(self):
        return {"Code": "0000", "Data": [{"Available": "10000000"}]}

    def QUERYRESTOREREPORT(self):
        return {"Code": "0000", "Data": []}

    def NEWORDER(self, payload):
        self.payloads.append(payload)
        return {"Code": "0000", "OrdNo": f"A{len(self.payloads)}"}

    def CANCELORDER(self, payload):
        self.cancels.append(payload["OrdNo"])
        return {"Code": "0000"}


class PositionEffectTests(unittest.TestCase):
    # TTB 2026.07.08 validates PositionEffect against {"Y", "N"} and rejects
    # the manual's 0/1/4 with 平倉格式錯誤; "Y" may not exceed the holding.
    def test_only_an_order_inside_the_opposite_position_closes(self):
        self.assertEqual(position_effect_for("buy", 1, 0), "N")
        self.assertEqual(position_effect_for("sell", 1, 0), "N")
        self.assertEqual(position_effect_for("sell", 1, 2), "Y")
        self.assertEqual(position_effect_for("buy", 2, -2), "Y")
        self.assertEqual(position_effect_for("buy", 1, 1), "N")
        self.assertEqual(position_effect_for("sell", 3, 2), "N")

    def test_request_defaults_to_open_and_rejects_vendor_manual_codes(self):
        self.assertEqual(TTBOrderRequest("TMFK6", "buy", 100).payload()["PositionEffect"], "N")
        for code in ("0", "1", "4"):
            with self.assertRaises(ValueError):
                TTBOrderRequest("TMFK6", "buy", 100, position_effect=code)

    def test_market_flatten_is_sent_as_close(self):
        module = FakeModule()
        api = TTBApi.__new__(TTBApi)
        api.module = module
        api.new_market_ioc("TMFK6", "sell", 1)
        self.assertEqual(module.payloads[0]["PositionEffect"], "Y")


class GatewayPositionEffectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = StrategyConfig(symbol="TXFA7", max_inventory=1, max_order_qty=1)
        self.module = FakeModule()
        self.ledger = PositionLedger(self.config)
        self.gateway = TTBOrderGateway(
            self.module, self.config, self.ledger, 0.25,
            risk_limits=PreTradeLimits(
                max_notional=5_000_000, minimum_available_margin=1000,
                available_margin_field="Available",
            ),
            risk_counters_path=Path(tmp.name) / "risk.json",
        )
        self.gateway.bootstrapped = True

    def test_exit_while_long_closes_and_entry_while_flat_opens(self):
        self.gateway.sync((OrderIntent("buy", 19990, 1, "entry", "test"),), quote())
        self.assertEqual(self.module.payloads[-1]["PositionEffect"], "N")
        self.ledger.on_fill(Fill("buy", 19990, 1, "A1", 101))
        self.gateway.active.clear()
        self.gateway.sync((OrderIntent("sell", 20010, 1, "exit", "test"),), quote())
        self.assertEqual(self.module.payloads[-1]["PositionEffect"], "Y")

    def test_working_open_order_is_replaced_once_it_would_close(self):
        self.gateway.sync((OrderIntent("sell", 20010, 1, "entry", "test"),), quote())
        self.assertEqual(self.gateway.active["sell"]["effect"], "N")
        self.ledger.on_fill(Fill("buy", 19990, 1, "X", 101))
        self.gateway.sync((OrderIntent("sell", 20010, 1, "exit", "test"),), quote())
        self.assertEqual(self.module.cancels, ["A1"])

    def _reject_cancel_with_report(self, status, remaining):
        self.gateway.sync((OrderIntent("buy", 19990, 1, "entry", "test"),), quote())
        self.module.CANCELORDER = lambda payload: {"Code": "9999", "ErrMsg": "cannot cancel"}
        self.module.QUERYRESTOREREPORT = lambda: {"Code": "0000", "Data": [{
            "ORDNO": "A1", "COMD_ID": "TXFA7", "BS": "B", "VOLM": "1",
            "LESS_VOLM": str(remaining), "DEAL_TOTL": str(1 - remaining),
            "DEAL_PRCE": "19990", "ORDR_PRCE": "19990", "STAT": status,
        }]}

    def test_cancel_rejected_for_an_order_that_already_filled_waits_for_its_report(self):
        self._reject_cancel_with_report("完全成交", 0)
        self.gateway.sync((OrderIntent("buy", 19980, 1, "entry", "test"),), quote())
        self.assertIn("A1", self.gateway.cancel_requested)
        self.assertEqual(len(self.module.payloads), 1)

    def test_cancel_rejected_for_a_still_working_order_stays_fatal(self):
        self._reject_cancel_with_report("委託成功", 1)
        with self.assertRaises(Exception):
            self.gateway.sync((OrderIntent("buy", 19980, 1, "entry", "test"),), quote())


if __name__ == "__main__":
    unittest.main()
