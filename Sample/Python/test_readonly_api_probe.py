import json
import unittest

from strategy_core import Quote
from ttb.api import TTBApi
from ttb.probe import OrderProbe
from ttb_readonly_capture import capture, sanitized_response
from ttb_order_probe import main as probe_main


class FakeVirtualApi:
    def __init__(self, orders=None, positions=None, margin=None, ack=None):
        self.orders = orders if orders is not None else []
        self.positions = positions if positions is not None else []
        self.margin = margin if margin is not None else [{"AvailableMargin": "100000"}]
        self.ack = ack if ack is not None else {"Code": "0000", "OrdNo": "TTB-123"}
        self.new_calls = 0
        self.cancel_calls = []
        self.queried = []
        self.acknowledged = False

    def QUERYRESTOREREPORT(self):
        self.queried.append("orders")
        return {"Code": "0000", "Data": self.orders}

    def QUERYRESTOREFILLREPORT(self):
        self.queried.append("positions")
        return {"Code": "0000", "Data": self.positions}

    def QUERYMARGIN(self):
        self.queried.append("margin")
        return {"Code": "0000", "Data": self.margin}

    def NEWORDER(self, payload):
        self.new_calls += 1
        self.acknowledged = True
        return self.ack

    def CANCELORDER(self, payload):
        self.cancel_calls.append(payload)
        return {"Code": "0000"}


def quote():
    return Quote(
        symbol="TXFA7", bid=20000, ask=20001, bid_qty=10, ask_qty=12,
        last=20000, last_qty=1, total_volume=100,
        exchange_time="09:00:00", received_at=1.0,
    )


class SaferProbeTests(unittest.TestCase):
    def test_account_wide_position_in_another_symbol_blocks_probe(self):
        fake = FakeVirtualApi(positions=[{
            "ComdID1": "MTXA7", "ComdBS1": "1", "Hold": "1",
        }])
        with self.assertRaisesRegex(RuntimeError, "not flat"):
            OrderProbe(TTBApi(fake), "TXFA7", poll_seconds=0.001).run(quote())
        self.assertEqual(fake.new_calls, 0)

    def test_unrelated_working_order_blocks_probe(self):
        fake = FakeVirtualApi(orders=[{
            "ORDNO": "MANUAL-A", "COMD_ID": "MTXA7",
            "VOLM": "1", "LESS_VOLM": "1", "STAT": "委託成功",
        }])
        with self.assertRaisesRegex(RuntimeError, "working orders"):
            OrderProbe(TTBApi(fake), "TXFA7", poll_seconds=0.001).run(quote())
        self.assertEqual(fake.new_calls, 0)

    def test_probe_without_order_no_cancels_its_report_row(self):
        fake = FakeVirtualApi(ack={"Code": "0000"})

        def new_order(payload):
            fake.new_calls += 1
            fake.orders.append({
                "ORDNO": "Dm1", "COMD_ID": "TXFA7", "BS": "買別", "ORDR_PRCE": payload["Price"],
                "VOLM": "1", "LESS_VOLM": "1", "DEAL_TOTL": "0", "STAT": "委託成功",
            })
            return {"Code": "0000"}

        def cancel(payload):
            fake.cancel_calls.append(payload)
            fake.orders[0].update(LESS_VOLM="0", STAT="刪單成功")
            return {"Code": "0000"}

        fake.NEWORDER, fake.CANCELORDER = new_order, cancel
        result = OrderProbe(TTBApi(fake), "TXFA7", poll_seconds=0.001).run(quote())
        self.assertEqual(result.order_no, "Dm1")
        self.assertEqual(fake.cancel_calls, [{"OrdNo": "Dm1"}])
        self.assertTrue(result.cancel_confirmed)

    def test_probe_order_missing_from_report_never_cancels_a_guess(self):
        fake = FakeVirtualApi(ack={"Code": "0000"})
        with self.assertRaisesRegex(RuntimeError, "report channel presumed lost"):
            OrderProbe(TTBApi(fake), "TXFA7", poll_seconds=0.001, ack_timeout=0.05).run(quote())
        self.assertEqual(fake.new_calls, 1)
        self.assertEqual(fake.cancel_calls, [])

    def test_broker_account_query_failure_blocks_before_order(self):
        fake = FakeVirtualApi()
        fake.QUERYRESTOREFILLREPORT = lambda: {"Code": "0000"}  # missing Data
        with self.assertRaisesRegex(RuntimeError, "missing Data"):
            OrderProbe(TTBApi(fake), "TXFA7", poll_seconds=0.001).run(quote())
        self.assertEqual(fake.new_calls, 0)

    def test_probe_cli_blocks_without_operator_bot_confirmation(self):
        with self.assertRaisesRegex(SystemExit, "confirm-bot-stopped"):
            probe_main(["--symbol", "TXFA7", "--confirm-virtual-order-probe", "--confirm-flat-account"])


class ReadOnlyFixtureTests(unittest.TestCase):
    def test_capture_calls_only_read_endpoints_and_hides_account_secrets(self):
        api = FakeVirtualApi(
            orders=[{
                "ORDNO": "SECRET-ORDER-123", "COMD_ID": "TXFA7", "LESS_VOLM": "1",
                "STAT": "委託成功", "CustomerName": "PRIVATE-NAME", "Account": "PRIVATE-ID",
            }],
            positions=[{"ComdID1": "TXFA7", "ComdBS1": "1", "Hold": "0",
                        "CustomerName": "PRIVATE-NAME"}],
            margin=[{"AvailableMargin": 1234567.89, "Account": "PRIVATE-ID"}],
        )
        data = capture(TTBApi(api))
        payload = json.dumps(data, ensure_ascii=False)
        for secret in ("SECRET-ORDER-123", "PRIVATE-NAME", "PRIVATE-ID", "1234567.89"):
            self.assertNotIn(secret, payload)
        self.assertEqual(data["account_identity"], "UNVERIFIED_MANUALLY_IN_GUI")
        self.assertEqual(data["orders"]["rows"][0]["COMD_ID"], "TXFA7")
        self.assertTrue(data["orders"]["rows"][0]["ORDNO"].startswith("hash:"))
        self.assertEqual(api.new_calls, 0)
        self.assertEqual(api.cancel_calls, [])
        self.assertEqual(api.queried, ["margin", "orders", "positions"])

    def test_capture_fails_closed_on_malformed_success_report(self):
        with self.assertRaisesRegex(RuntimeError, "missing Data"):
            sanitized_response("orders", {"Code": "0000"}, "salt")

    def test_margin_stores_schema_types_not_amounts(self):
        result = sanitized_response("margin", {
            "Code": "0000", "Data": [{"AvailableMargin": "9876543.21"}],
        }, "salt")
        self.assertEqual(result["fields"], [{"AvailableMargin": "str"}])
        self.assertNotIn("9876543.21", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
