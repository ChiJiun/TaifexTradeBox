import unittest

from strategies import MovingAverageStrategy
from strategy_core import OrderIntent, PositionLedger, Quote, StrategyConfig
from ttb.api import TTBApi, TTBOrderRequest, extract_order_no
from ttb.market_data import QuoteMonitor
from ttb.orders import TTBOrderGateway
from ttb.probe import OrderProbe, far_limit_price
from sim_hft_bot import build_strategy, parse_args


def quote(last=20_000, received_at=100.0):
    return Quote(
        symbol="TXFA7",
        bid=last,
        ask=last + 1,
        bid_qty=10,
        ask_qty=10,
        last=last,
        last_qty=1,
        total_volume=received_at,
        exchange_time="09:00:00.000",
        received_at=received_at,
    )


class CustomDeployStrategy:
    def __init__(self, config, ledger):
        self.config = config
        self.ledger = ledger

    def decide(self, _quote):
        raise NotImplementedError


class FakeModule:
    def __init__(self):
        self.calls = []

    def _ok(self, name, payload=None):
        self.calls.append((name, payload))
        return {"Code": "0000"}

    def QUOTEDATA(self, symbol):
        self.calls.append(("quote", symbol))
        return {"Code": "0000"}

    def NEWORDER(self, payload):
        response = self._ok("new", payload)
        response["OrdNo"] = "A001"
        return response

    def CANCELORDER(self, payload):
        return self._ok("cancel", payload)

    def REPLACEPRICE(self, payload):
        return self._ok("replace_price", payload)

    def REPLACEQTY(self, payload):
        return self._ok("replace_qty", payload)

    def QUERYMARGIN(self):
        return self._ok("margin")

    def QUERYRESTOREFILLREPORT(self):
        return self._ok("fills")

    def QUERYRESTOREREPORT(self):
        return {"Code": "0000", "Data": []}


class ApiTests(unittest.TestCase):
    def test_extract_order_number_from_nested_json_data(self):
        self.assertEqual(extract_order_no({"Code": "0000", "Data": '{"OrdNo":"A009"}'}), "A009")

    def test_order_functions_build_official_payloads(self):
        module = FakeModule()
        api = TTBApi(module)
        api.new_order(TTBOrderRequest("txfa7", "buy", 20_000, 2))
        api.replace_price("A001", 20_001)
        api.replace_quantity("A001", 1)
        api.cancel_order("A001")

        self.assertEqual(module.calls[0][1]["Symbol1"], "TXFA7")
        self.assertEqual(module.calls[0][1]["Side1"], "1")
        self.assertEqual(module.calls[0][1]["OrderQty"], "2")
        self.assertIn(("replace_price", {"OrdNo": "A001", "Price": "20001"}), module.calls)
        self.assertIn(("replace_qty", {"OrdNo": "A001", "UdpQty": "1"}), module.calls)
        self.assertIn(("cancel", {"OrdNo": "A001"}), module.calls)

    def test_gateway_turns_strategy_intent_into_order(self):
        module = FakeModule()
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=1)
        gateway = TTBOrderGateway(module, config, PositionLedger(config), 0.5)
        gateway.bootstrapped = True
        gateway.sync((OrderIntent("sell", 20_010, 1, "entry", "test"),), quote())

        self.assertEqual(gateway.active["sell"]["order_no"], "A001")
        payload = next(payload for name, payload in module.calls if name == "new")
        self.assertEqual(payload["Side1"], "2")
        self.assertEqual(payload["Price"], "20010")

    def test_gateway_emergency_flatten_reconciles_broker_fill(self):
        class FlattenModule(FakeModule):
            def __init__(self):
                super().__init__()
                self.flatten_sent = False

            def NEWORDER(self, payload):
                response = super().NEWORDER(payload)
                self.flatten_sent = True
                return response

            def QUERYRESTOREFILLREPORT(self):
                if self.flatten_sent:
                    return {"Code": "0000", "Data": []}
                return {
                    "Code": "0000",
                    "Data": [{"Hold": "1", "ComdID1": "TXFA7", "ComdBS1": "1"}],
                }

            def QUERYRESTOREREPORT(self):
                if not self.flatten_sent:
                    return {"Code": "0000", "Data": []}
                return {
                    "Code": "0000",
                    "Data": [{
                        "ORDNO": "A001",
                        "COMD_ID": "TXFA7",
                        "BS": "2",
                        "ORDR_PRCE": "20000",
                        "VOLM": "1",
                        "LESS_VOLM": "0",
                        "DEAL_TOTL": "1",
                        "DEAL_PRCE": "20000",
                        "STAT": "完全成交",
                    }],
                }

        module = FlattenModule()
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=1)
        ledger = PositionLedger(config)
        ledger.position = 1
        ledger.average_price = 19_999
        gateway = TTBOrderGateway(module, config, ledger, 0.01)
        gateway.bootstrapped = True
        gateway.reported_position = 1

        self.assertTrue(gateway.emergency_flatten(quote(), timeout_seconds=1.0))
        self.assertEqual(ledger.position, 0)
        payload = next(payload for name, payload in module.calls if name == "new")
        self.assertEqual(payload["Side1"], "2")
        self.assertEqual(payload["TimeInForce"], "2")

    def test_gateway_refuses_startup_with_existing_working_order(self):
        module = FakeModule()
        module.QUERYRESTOREREPORT = lambda: {
            "Code": "0000",
            "Data": [
                {
                    "ORDNO": "OLD1",
                    "COMD_ID": "TXFA7",
                    "VOLM": "1",
                    "LESS_VOLM": "1",
                    "STAT": "委託成功",
                }
            ],
        }
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=1)
        gateway = TTBOrderGateway(module, config, PositionLedger(config), 0.25)
        with self.assertRaisesRegex(RuntimeError, "Working orders already exist"):
            gateway.before_decision(quote())


class OrderProbeTests(unittest.TestCase):
    def test_far_buy_price_is_below_bid(self):
        self.assertEqual(far_limit_price(quote(), "buy", 1, 2000), 18000)

    def test_probe_posts_then_cancels_one_order(self):
        class ProbeModule(FakeModule):
            def __init__(self):
                super().__init__()
                self.cancelled = False

            def CANCELORDER(self, payload):
                self.cancelled = True
                return self._ok("cancel", payload)

            def QUERYRESTOREREPORT(self):
                if not any(name == "new" for name, _ in self.calls):
                    return {"Code": "0000", "Data": []}
                return {
                    "Code": "0000",
                    "Data": [
                        {
                            "ORDNO": "A001",
                            "COMD_ID": "TXFA7",
                            "BS": "1",
                            "ORDR_PRCE": "18000",
                            "VOLM": "1",
                            "LESS_VOLM": "0" if self.cancelled else "1",
                            "STAT": "取消" if self.cancelled else "委託成功",
                        }
                    ],
                }

        module = ProbeModule()
        result = OrderProbe(
            TTBApi(module),
            "TXFA7",
            distance_ticks=2000,
            ack_timeout=0.1,
            cancel_timeout=0.1,
            poll_seconds=0.01,
        ).run(quote())
        self.assertTrue(result.order_accepted)
        self.assertTrue(result.cancel_accepted)
        self.assertTrue(result.cancel_confirmed)
        self.assertTrue(result.fill_query_succeeded)
        self.assertEqual(result.unexpected_fill_quantity, 0)
        self.assertIn(("cancel", {"OrdNo": "A001"}), module.calls)

    def test_probe_records_failed_fill_reconciliation(self):
        class ProbeModule(FakeModule):
            def __init__(self):
                super().__init__()
                self.cancelled = False

            def CANCELORDER(self, payload):
                self.cancelled = True
                return self._ok("cancel", payload)

            def QUERYRESTOREREPORT(self):
                if not any(name == "new" for name, _ in self.calls):
                    return {"Code": "0000", "Data": []}
                return {
                    "Code": "0000",
                    "Data": [
                        {
                            "ORDNO": "A001",
                            "COMD_ID": "TXFA7",
                            "BS": "1",
                            "ORDR_PRCE": "18000",
                            "VOLM": "1",
                            "LESS_VOLM": "0" if self.cancelled else "1",
                            "DEAL_TOTL": "0",
                            "STAT": "取消" if self.cancelled else "委託成功",
                        }
                    ],
                }

            def QUERYRESTOREFILLREPORT(self):
                return {"Code": "9999", "ErrMsg": "401 Unauthorized"}

        result = OrderProbe(
            TTBApi(ProbeModule()),
            "TXFA7",
            distance_ticks=2000,
            ack_timeout=0.1,
            cancel_timeout=0.1,
            poll_seconds=0.01,
        ).run(quote())
        self.assertTrue(result.cancel_confirmed)
        self.assertFalse(result.fill_query_succeeded)
        self.assertEqual(result.unexpected_fill_quantity, 0)
        self.assertIn("verification failed", result.message)


class MonitorTests(unittest.TestCase):
    def test_monitor_registers_symbol_and_emits_typed_quote(self):
        module = FakeModule()
        received = []

        class FakeSubscriber:
            def __init__(self, port, symbol, callback):
                self.port = port
                self.symbol = symbol
                self.callback = callback
                self.started = False

            def start(self):
                self.started = True

            def close(self):
                pass

        monitor = QuoteMonitor(TTBApi(module), 51141, "txfa7", received.append, FakeSubscriber)
        monitor.start()
        monitor.feed(
            {
                "Symbol": "TXFA7",
                "BidPs": "19,999",
                "AskPs": "20,000",
                "BidPv": "10",
                "AskPv": "8",
                "Price": "20,000",
                "Qty": "1",
                "Volume": "100",
                "TickTime": "09:00:00.000",
            }
        )
        monitor.feed({"Symbol": "OTHER"})

        self.assertIn(("quote", "TXFA7"), module.calls)
        self.assertTrue(monitor.subscriber.started)
        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], Quote)


class ExampleStrategyTests(unittest.TestCase):
    def test_custom_strategy_module_can_be_loaded_without_editing_runner(self):
        args = parse_args([
            "--symbol", "TXFA7",
            "--strategy-module", "test_ttb_modules:CustomDeployStrategy",
        ])
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=1)
        ledger = PositionLedger(config)
        strategy = build_strategy(args, config, ledger)
        self.assertIsInstance(strategy, CustomDeployStrategy)
        self.assertIs(strategy.config, config)
        self.assertIs(strategy.ledger, ledger)

    def test_moving_average_strategy_emits_order_intent(self):
        config = StrategyConfig(symbol="TXFA7", base_qty=1, max_order_qty=1, max_inventory=1)
        strategy = MovingAverageStrategy(config, PositionLedger(config), short_window=2, long_window=3, entry_ticks=0)
        strategy.decide(quote(20_000, 100))
        strategy.decide(quote(20_001, 101))
        decision = strategy.decide(quote(20_002, 102))

        self.assertEqual(decision.reason, "moving-average long")
        self.assertEqual(decision.intents[0].side, "buy")
        self.assertEqual(decision.intents[0].role, "entry")


if __name__ == "__main__":
    unittest.main()
