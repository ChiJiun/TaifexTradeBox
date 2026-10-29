import unittest
import tempfile
from pathlib import Path

from strategies import MovingAverageStrategy
from strategy_core import OrderIntent, PositionLedger, Quote, StrategyConfig
from ttb.api import TTBApi, TTBOrderRequest, extract_order_no
from ttb.market_data import QuoteMonitor
from ttb.orders import TTBOrderGateway
from ttb.probe import OrderProbe, far_limit_price
from ttb.risk import PreTradeLimits
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
        self.calls.append(("margin", None))
        return {"Code": "0000", "Data": [{"Available": "10000000"}]}

    def QUERYRESTOREFILLREPORT(self):
        response = self._ok("fills")
        response["Data"] = []
        return response

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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway = TTBOrderGateway(
            module, config, PositionLedger(config), 0.5,
            risk_limits=PreTradeLimits(
                max_notional=100000000,
                available_margin_field="Available",
                minimum_available_margin=1000,
            ),
            risk_counters_path=Path(tmp.name) / "rates.json",
        )
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway = TTBOrderGateway(
            module, config, ledger, 0.01,
            risk_counters_path=Path(tmp.name) / "rates.json",
        )
        gateway.bootstrapped = True
        gateway.reported_position = 1

        self.assertTrue(gateway.emergency_flatten(quote(), timeout_seconds=1.0))
        self.assertEqual(ledger.position, 0)
        payload = next(payload for name, payload in module.calls if name == "new")
        self.assertEqual(payload["Side1"], "2")
        self.assertEqual(payload["TimeInForce"], "2")

    def test_gateway_refuses_old_contract_exposure_before_manual_rollover(self):
        for mode in ("working", "position"):
            with self.subTest(mode=mode):
                module = FakeModule()
                if mode == "working":
                    module.QUERYRESTOREREPORT = lambda: {
                        "Code": "0000",
                        "Data": [{
                            "ORDNO": "OLD", "COMD_ID": "TMFJ6", "VOLM": "1",
                            "LESS_VOLM": "1", "STAT": "委託成功",
                        }],
                    }
                else:
                    module.QUERYRESTOREFILLREPORT = lambda: {
                        "Code": "0000",
                        "Data": [{"Hold": "1", "ComdID1": "TMFJ6", "ComdBS1": "1"}],
                    }
                config = StrategyConfig(symbol="TMFK6")
                with tempfile.TemporaryDirectory() as folder:
                    gateway = TTBOrderGateway(
                        module, config, PositionLedger(config), 0.25,
                        risk_counters_path=Path(folder) / "rates.json",
                    )
                    with self.assertRaisesRegex(RuntimeError, "TTB account"):
                        gateway.before_decision(quote())

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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway = TTBOrderGateway(
            module, config, PositionLedger(config), 0.25,
            risk_counters_path=Path(tmp.name) / "rates.json",
        )
        with self.assertRaisesRegex(RuntimeError, "Working orders already exist"):
            gateway.before_decision(quote())



class LiveOwnershipSafetyTests(unittest.TestCase):
    def setup_gateway(self):
        module = FakeModule()
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=1)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway = TTBOrderGateway(
            module, config, PositionLedger(config), 0.25,
            risk_limits=PreTradeLimits(
                max_notional=100000000,
                available_margin_field="Available",
                minimum_available_margin=1000,
            ),
            risk_counters_path=Path(tmp.name) / "rates.json",
        )
        gateway.before_decision(quote())  # establish a flat/empty baseline
        return module, gateway

    @staticmethod
    def row(order_no="MANUAL1", symbol="TXFA7", side="1", price="20000", filled="0",
            remaining="1"):
        return {
            "ORDNO": order_no, "COMD_ID": symbol, "BS": side,
            "ORDR_PRCE": price, "VOLM": "1", "LESS_VOLM": remaining,
            "DEAL_TOTL": filled,
            "DEAL_PRCE": price if filled != "0" else "0",
            "STAT": "委託成功" if remaining != "0" else "完全成交",
        }

    def poll(self, module, gateway, rows):
        module.QUERYRESTOREREPORT = lambda: {"Code": "0000", "Data": rows}
        gateway.last_poll = 0.0
        return gateway.before_decision(quote())

    def test_new_manual_working_order_is_latched_and_blocks_future_orders(self):
        module, gateway = self.setup_gateway()
        with self.assertRaisesRegex(RuntimeError, "Unowned TTB"):
            self.poll(module, gateway, [self.row()])
        self.assertEqual(gateway.snapshot()["unknown_orders"], ["MANUAL1"])
        self.assertTrue(gateway.snapshot()["unsafe_reason"])
        with self.assertRaisesRegex(RuntimeError, "Unowned TTB"):
            gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.assertFalse(any(name == "new" for name, _ in module.calls))
        self.assertFalse(any(name == "cancel" for name, _ in module.calls))

    def test_sent_order_binds_to_the_one_new_matching_report_row(self):
        # TTB 2026.07.08 NEWORDER replies carry no OrdNo; the bot-exclusive
        # account makes the new report row the order's only possible owner.
        module, gateway = self.setup_gateway()
        module.NEWORDER = lambda payload: {"Code": "0000"}
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.assertIn("buy", gateway.pending)
        self.assertFalse(gateway.snapshot()["unsafe_reason"])
        self.poll(module, gateway, [self.row(order_no="BOUND1", price="20,000.00")])
        self.assertEqual(gateway.pending, {})
        self.assertEqual(gateway.active["buy"]["order_no"], "BOUND1")
        self.assertIn("BOUND1", gateway.owned_order_nos)

    def test_old_row_already_in_the_report_is_never_bound_to_a_new_order(self):
        module, gateway = self.setup_gateway()
        old = self.row(order_no="OLD1", remaining="0")
        old["STAT"] = "刪單成功"
        self.poll(module, gateway, [old])
        module.NEWORDER = lambda payload: {"Code": "0000"}
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        self.poll(module, gateway, [old])
        self.assertIn("buy", gateway.pending)
        self.poll(module, gateway, [old, self.row(order_no="NEW1")])
        self.assertEqual(gateway.active["buy"]["order_no"], "NEW1")

    def test_row_with_unreadable_side_never_binds(self):
        from ttb.orders import find_sent_order
        row = self.row(order_no="X1", side="?")
        self.assertEqual(find_sent_order([row], set(), "TXFA7", "sell", 20000, 1), "")

    def test_two_new_rows_matching_one_sent_order_latch(self):
        module, gateway = self.setup_gateway()
        module.NEWORDER = lambda payload: {"Code": "0000"}
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        with self.assertRaisesRegex(RuntimeError, "Several new TTB orders"):
            self.poll(module, gateway, [self.row(order_no="A"), self.row(order_no="B")])

    def test_new_row_that_differs_from_the_sent_order_is_unowned(self):
        module, gateway = self.setup_gateway()
        module.NEWORDER = lambda payload: {"Code": "0000"}
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        with self.assertRaisesRegex(RuntimeError, "Unowned TTB"):
            self.poll(module, gateway, [self.row(price="19999")])
        self.assertIn("buy", gateway.pending)

    def test_sent_order_missing_from_report_stops_the_bot(self):
        module, gateway = self.setup_gateway()
        module.NEWORDER = lambda payload: {"Code": "0000"}
        gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        gateway.pending["buy"]["created_at"] -= 11
        with self.assertRaisesRegex(RuntimeError, "report channel presumed lost"):
            gateway.sync((OrderIntent("buy", 20000, 1, "entry", "test"),), quote())
        sent = []
        module.NEWORDER = lambda payload: sent.append(payload) or {"Code": "0000"}
        # The unseen order may still be live: a shutdown flatten must not trade around it.
        self.assertFalse(gateway.emergency_flatten(quote(), timeout_seconds=0.01))
        self.assertEqual(sent, [])

    def test_known_bot_order_can_be_polled_normally(self):
        module, gateway = self.setup_gateway()
        gateway.owned_order_nos.add("OWNED1")
        self.poll(module, gateway, [self.row(order_no="OWNED1")])
        self.assertEqual(gateway.active["buy"]["order_no"], "OWNED1")
        self.assertEqual(gateway.unknown_order_nos, set())

    def test_other_symbol_and_preexisting_terminal_orders_are_ignored(self):
        module = FakeModule()
        module.QUERYRESTOREREPORT = lambda: {
            "Code": "0000",
            "Data": [self.row(order_no="HISTORIC", remaining="0", filled="1")],
        }
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=1)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway = TTBOrderGateway(
            module, config, PositionLedger(config), 0.25,
            risk_counters_path=Path(tmp.name) / "rates.json",
        )
        gateway.before_decision(quote())
        self.poll(module, gateway, [
            self.row(order_no="HISTORIC", remaining="0", filled="1"),
            self.row(order_no="OTHER", symbol="TMFA7"),
        ])
        self.assertFalse(gateway.unsafe_reason)

    def test_unowned_filled_order_fails_even_when_no_working_order_remains(self):
        module, gateway = self.setup_gateway()
        with self.assertRaisesRegex(RuntimeError, "Unowned TTB"):
            self.poll(module, gateway, [self.row(remaining="0", filled="1")])

    def test_duplicate_broker_fill_report_is_journaled_once(self):
        module, gateway = self.setup_gateway()
        gateway.owned_order_nos.add("OWNED1")
        module.QUERYRESTOREFILLREPORT = lambda: {
            "Code": "0000",
            "Data": [{"Hold": "1", "ComdID1": "TXFA7", "ComdBS1": "1"}],
        }
        filled = self.row(order_no="OWNED1", filled="1", remaining="0")
        self.poll(module, gateway, [filled])
        self.poll(module, gateway, [filled])
        self.assertEqual(gateway.ledger.position, 1)
        self.assertEqual(gateway.ledger.fill_count, 1)
        events = gateway.journal.events(gateway.journal.local_day())
        self.assertEqual([event["kind"] for event in events].count("fill"), 1)

    def test_two_bot_owned_working_orders_on_same_side_fail_closed(self):
        module, gateway = self.setup_gateway()
        gateway.owned_order_nos.update({"ONE", "TWO"})
        with self.assertRaisesRegex(RuntimeError, "Multiple bot-owned working orders"):
            self.poll(module, gateway, [
                self.row(order_no="ONE"),
                self.row(order_no="TWO"),
            ])


class PlaceQueryReuseTests(unittest.TestCase):
    """_place reuses fresh reads instead of 0.5 s+ TTB queries per order."""

    def setup_gateway(self, margin_cache_seconds=5.0):
        module = FakeModule()
        module.rows = []

        def orders():
            module.calls.append(("orders", None))
            return {"Code": "0000", "Data": module.rows}

        module.QUERYRESTOREREPORT = orders
        module.NEWORDER = lambda payload: module.calls.append(("new", payload)) or {"Code": "0000"}
        config = StrategyConfig(symbol="TXFA7", max_order_qty=1, max_inventory=3)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway = TTBOrderGateway(
            module, config, PositionLedger(config), 0.25,
            risk_limits=PreTradeLimits(
                max_notional=100000000,
                available_margin_field="Available",
                minimum_available_margin=1000,
            ),
            risk_counters_path=Path(tmp.name) / "rates.json",
            margin_cache_seconds=margin_cache_seconds,
        )
        gateway.before_decision(quote())
        return module, gateway

    @staticmethod
    def count(module, name):
        return sum(1 for call, _ in module.calls if call == name)

    def place(self, gateway, *intents):
        gateway.pending.clear()
        gateway.active.clear()
        gateway.sync(tuple(intents), quote())

    def test_poll_reads_positions_before_the_order_report(self):
        module, gateway = self.setup_gateway()
        polled = [name for name, _ in module.calls if name in ("fills", "orders")]
        self.assertEqual(polled, ["fills", "orders"])

    def test_order_right_after_a_poll_binds_against_that_poll(self):
        module, gateway = self.setup_gateway()
        before = self.count(module, "orders")
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "orders"), before)
        self.assertEqual(self.count(module, "new"), 1)

    def test_report_reuse_ends_at_half_a_second(self):
        module, gateway = self.setup_gateway()
        gateway._report_read_at = __import__("time").monotonic() - 0.4
        before = self.count(module, "orders")
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "orders"), before)
        gateway._report_read_at = __import__("time").monotonic() - 0.51
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "orders"), before + 1)

    def test_shutdown_flatten_always_reads_the_report_fresh(self):
        module, gateway = self.setup_gateway()
        gateway.ledger.position = 1
        module.QUERYRESTOREFILLREPORT = lambda: {
            "Code": "0000", "Data": [{"Hold": "1", "ComdID1": "TXFA7", "ComdBS1": "1"}],
        }
        late = LiveOwnershipSafetyTests.row(order_no="LATE", side="2", remaining="0")
        late["STAT"] = "刪單成功"
        module.rows = [late]
        # A reused read would miss LATE; the flatten must take a fresh one.
        gateway._report_order_nos = set()
        gateway._report_read_at = float("inf")
        self.assertFalse(gateway.emergency_flatten(quote(), timeout_seconds=0.01))
        self.assertIn("LATE", gateway.pending["sell"]["known"])

    def test_stale_poll_takes_a_fresh_snapshot_that_excludes_late_rows(self):
        module, gateway = self.setup_gateway()
        gateway._report_read_at -= 1.0
        # A same-price row that reached the report after the last poll, e.g. a
        # slow fresh-login backfill, must never be bound to the next order.
        late = LiveOwnershipSafetyTests.row(order_no="LATE", remaining="0")
        late["STAT"] = "刪單成功"
        module.rows = [late]
        before = self.count(module, "orders")
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "orders"), before + 1)
        self.assertIn("LATE", gateway.pending["buy"]["known"])

    def test_entries_reuse_margin_while_exposure_does_not_grow(self):
        module, gateway = self.setup_gateway()
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.place(gateway, OrderIntent("buy", 19990, 1, "entry", "test"))
        self.assertEqual(self.count(module, "new"), 2)
        self.assertEqual(self.count(module, "margin"), 1)

    def test_working_or_unbound_entry_forces_a_fresh_margin_read(self):
        # TTB may reserve margin for working orders; a read taken before the
        # buy was sent cannot vouch for the sell sent next to it.
        module, gateway = self.setup_gateway()
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"),
                   OrderIntent("sell", 20010, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 2)
        read_at, _exposure, response = gateway._margin_cache
        gateway._margin_cache = (read_at, 0, response)
        working = {"buy": {"order_no": "W1", "price": 20000, "qty": 1, "effect": "N"}}
        both = (OrderIntent("buy", 20000, 1, "entry", "test"),
                OrderIntent("sell", 20010, 1, "entry", "test"))
        for expected in (3, 3):  # a working buy forces one read, then it is reused
            gateway.pending.clear()
            gateway.active = dict(working)
            gateway.sync(both, quote())
            self.assertEqual(self.count(module, "margin"), expected)

    def test_position_growth_either_way_forces_a_fresh_margin_read(self):
        module, gateway = self.setup_gateway()
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        gateway.ledger.position = 1
        self.place(gateway, OrderIntent("buy", 19990, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 2)
        gateway.ledger.position = 0
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 2)
        gateway.ledger.position = -2
        self.place(gateway, OrderIntent("sell", 20010, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 3)

    def test_margin_cache_seconds_must_be_bounded(self):
        for bad in (-1, float("inf"), float("nan"), 31):
            with self.assertRaises(ValueError):
                self.setup_gateway(margin_cache_seconds=bad)

    def test_expired_or_disabled_margin_cache_reads_again(self):
        module, gateway = self.setup_gateway()
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        read_at, exposure, response = gateway._margin_cache
        gateway._margin_cache = (read_at - 5.0, exposure, response)
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 2)

        module, gateway = self.setup_gateway(margin_cache_seconds=0)
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 2)

    def test_failed_margin_read_is_not_cached(self):
        from ttb.risk import PreTradeRiskError
        module, gateway = self.setup_gateway()
        good = module.QUERYMARGIN
        module.QUERYMARGIN = lambda: module.calls.append(("margin", None)) or {"Code": "9999"}
        with self.assertRaises(PreTradeRiskError):
            self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        module.QUERYMARGIN = good
        self.place(gateway, OrderIntent("buy", 20000, 1, "entry", "test"))
        self.assertEqual(self.count(module, "margin"), 2)
        self.assertEqual(self.count(module, "new"), 1)


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
        self.assertTrue(result.position_query_succeeded)
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

        module = ProbeModule()
        with self.assertRaisesRegex(RuntimeError, "success code"):
            OrderProbe(
                TTBApi(module),
                "TXFA7",
                distance_ticks=2000,
                ack_timeout=0.1,
                cancel_timeout=0.1,
                poll_seconds=0.01,
            ).run(quote())
        self.assertFalse(any(name == "new" for name, _ in module.calls))


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
