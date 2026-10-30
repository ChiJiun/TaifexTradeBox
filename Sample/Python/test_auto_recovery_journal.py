"""Regressions from the 2026-10-30 CCFK6 stop: shutdown flatten, partial IOC recovery, journal."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from auto_recover_ttb import main as auto_recover_main
from strategy_core import PositionLedger, Quote, StrategyConfig
from test_competition_automation import FAST, FakeBroker
from ttb.auto_recovery import AttemptBudget, RecoveryPolicy, RecoveryRefused, auto_recover
from ttb.engine import BotEngine
from ttb.journal import ExecutionJournal, JournalError, JournalLock
from ttb.orders import TTBOrderGateway

DAY = "2026-10-30"
SYMBOL = "CCFK6"
TAIPEI = timezone(timedelta(hours=8))


def short(qty="3"):
    return {"ComdID1": SYMBOL, "ComdBS1": "S", "Hold": qty}


def bot_order_row(order_no="S1", side="S", qty="3", price="110"):
    """The bot's own opening order, final in the day's report."""
    return {"ORDNO": order_no, "COMD_ID": SYMBOL, "BS": side, "ORDR_PRCE": price, "VOLM": qty,
            "LESS_VOLM": "0", "DEAL_TOTL": qty, "DEAL_PRCE": price, "STAT": "完全成交"}


class ThinBookModule:
    """TTB with a CCF-like book: few resting lots per price; limit IOCs fill and the rest is deleted."""

    def __init__(self, position, asks):
        self.position = position          # signed
        self.asks = [list(level) for level in asks]  # [price, lots], best first
        self.rows = []
        self.sent = []

    def NEWORDER(self, payload):
        price, qty = float(payload["Price"]), int(payload["OrderQty"])
        assert payload["Side1"] == "1" and payload["TimeInForce"] == "2"
        self.sent.append((price, qty))
        filled, cost = 0, 0.0
        for level in self.asks:
            if level[0] > price or filled == qty:
                break
            take = min(level[1], qty - filled)
            level[1] -= take
            filled += take
            cost += take * level[0]
        self.asks = [level for level in self.asks if level[1]]
        self.position += filled
        self.rows.append({
            "ORDNO": f"B{len(self.sent):04d}", "COMD_ID": SYMBOL, "BS": "B", "ORDR_PRCE": payload["Price"],
            "VOLM": str(qty), "LESS_VOLM": "0", "DEAL_TOTL": str(filled),
            "DEAL_PRCE": str(cost / filled) if filled else "0", "STAT": "完全成交" if filled == qty else "已刪除",
        })
        return {"Code": "0000"}  # like TTB: OrdNo only in the report

    def CANCELORDER(self, payload):
        return {"Code": "0000"}

    def QUERYMARGIN(self):
        return {"Code": "0000", "Data": []}

    def QUERYRESTOREREPORT(self):
        return {"Code": "0000", "Data": list(self.rows)}

    def QUERYRESTOREFILLREPORT(self):
        if not self.position:
            return {"Code": "0000", "Data": []}
        side = "B" if self.position > 0 else "S"
        return {"Code": "0000", "Data": [{"ComdID1": SYMBOL, "ComdBS1": side, "Hold": str(abs(self.position))}]}


class ShutdownFlattenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = StrategyConfig(symbol=SYMBOL, tick_size=0.5, point_value=2000,
                                     max_order_qty=8, max_inventory=8)

    def gateway(self, module):
        ledger = PositionLedger(self.config)
        ledger.position, ledger.average_price = module.position, 110.0
        gateway = TTBOrderGateway(module, self.config, ledger, 0.01,
                                  risk_counters_path=Path(self.tmp.name) / "rates.json")
        gateway.bootstrapped = True
        gateway.reported_position = module.position
        return gateway

    def test_thin_book_shutdown_flatten_crosses_and_resends_the_rest(self):
        # 11:47: short 3, last trade 109.0, but only 1 lot at 109.5, 1 at 110.0
        # and 1 at 111.0 — one IOC at the stale touch (109.0) filled nothing.
        module = ThinBookModule(-3, [(109.5, 1), (110.0, 1), (111.0, 1)])
        gateway = self.gateway(module)
        quote = Quote(SYMBOL, bid=108.5, ask=109.0, bid_qty=1, ask_qty=1, last=109.0, last_qty=1,
                      total_volume=10, exchange_time="11:47:00.000", received_at=100.0)

        self.assertTrue(gateway.emergency_flatten(quote, timeout_seconds=10.0))
        self.assertEqual(gateway.ledger.position, 0)
        self.assertEqual(module.position, 0)
        # +2 ticks filled two lots, the 1-lot rest went out +5 ticks.
        self.assertEqual(module.sent, [(110.0, 3), (111.5, 1)])
        # Never more than 3% through the touch (inside the 10% daily limit).
        self.assertTrue(all(price <= 109.0 * 1.03 for price, _ in module.sent))
        fills = [e["payload"] for e in gateway.journal.events(gateway.journal.local_day()) if e["kind"] == "fill"]
        self.assertEqual(sum(fill["quantity"] for fill in fills), 3)

    def test_flatten_attempts_are_bounded(self):
        module = ThinBookModule(-3, [])  # nobody sells at any price
        gateway = self.gateway(module)
        quote = Quote(SYMBOL, bid=108.5, ask=109.0, bid_qty=1, ask_qty=1, last=109.0, last_qty=1,
                      total_volume=10, exchange_time="11:47:00.000", received_at=100.0)

        self.assertFalse(gateway.emergency_flatten(quote, timeout_seconds=4.0))
        # +2, +5 and +10 ticks (the last capped at 3% = 6 ticks), then stop.
        self.assertEqual(module.sent, [(110.0, 3), (111.5, 3), (112.0, 3)])


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.flag = root / "manual-reconciliation-required.flag"
        self.flag.write_text("x", encoding="ascii")
        self.audit = root / "audit"
        self.budget_file = root / "budget.json"
        self.journal_dir = root / "journal"

    def recover(self, broker, **kwargs):
        return auto_recover(
            broker, flag=self.flag, audit_dir=self.audit, budget=AttemptBudget(self.budget_file, 3),
            policy=FAST, market_open=True, sleep=lambda _: None, today=DAY, **kwargs,
        )

    def audit_text(self):
        return "".join(path.read_text(encoding="utf-8") for path in self.audit.iterdir())


class PartialMarketIocTests(RecoveryCase):
    def test_partially_filled_market_ioc_resends_the_rest_until_flat(self):
        # Dm35148b bought 2 of 3; the old code refused right there.
        broker = FakeBroker(positions=[short("3")], liquidity=[2, 1])
        result = self.recover(broker)
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(broker.market_orders, [(SYMBOL, "buy", 3), (SYMBOL, "buy", 1)])
        self.assertEqual(broker.positions, [])
        self.assertFalse(self.flag.exists())

    def test_lagging_position_report_never_resends_closed_lots(self):
        broker = FakeBroker(positions=[short("3")], liquidity=[2, 1], position_lag_polls=5)
        self.assertEqual(self.recover(broker)["action"], "recovered")
        self.assertEqual(broker.market_orders, [(SYMBOL, "buy", 3), (SYMBOL, "buy", 1)])
        self.assertEqual(broker.positions, [])

    def test_resends_are_bounded_and_keep_the_latch(self):
        broker = FakeBroker(positions=[short("3")], liquidity=[1, 0, 0, 3])
        with self.assertRaises(RecoveryRefused):
            self.recover(broker)
        self.assertEqual(broker.market_orders, [(SYMBOL, "buy", 3), (SYMBOL, "buy", 2), (SYMBOL, "buy", 2)])
        self.assertTrue(self.flag.exists())
        self.assertIn("auto_recovery_failed", self.audit_text())


class JournalReconciliationTests(RecoveryCase):
    def bot_journal(self, quantity=3):
        """What the bot journaled before it stopped: a short of `quantity` lots."""
        journal = ExecutionJournal(self.journal_dir, SYMBOL)
        journal.append("order_ack", {"order_no": "S1", "side": "sell", "quantity": quantity, "price": 110.0,
                                     "role": "entry", "source": "report"}, DAY)
        journal.append("fill", {"order_id": "S1", "side": "sell", "price": 110.0,
                                "quantity": quantity, "deal_total": quantity}, DAY)
        journal.append("shutdown", {"position": -quantity, "fatal_error": "", "reconciled": False}, DAY)
        return journal

    def restart_engine(self):
        """The bot's same-day startup replay (raises on an open journal position)."""
        config = StrategyConfig(symbol=SYMBOL, tick_size=0.5, point_value=2000, max_order_qty=8, max_inventory=8)
        gateway = TTBOrderGateway(ThinBookModule(0, []), config, PositionLedger(config), 0.25,
                                  risk_counters_path=Path(self.tmp.name) / "rates.json",
                                  journal_dir=self.journal_dir)
        engine = BotEngine(config, gateway, Path(self.tmp.name) / "run",
                           risk_state_path=Path(self.tmp.name) / "risk-state.json")
        self.addCleanup(engine.recorder.close)
        self.addCleanup(engine.raw_recorder.close)
        engine._restore_or_roll_risk_state(datetime(2026, 10, 30, 12, 0, tzinfo=TAIPEI))
        return engine

    def recovery_fills(self):
        journal = ExecutionJournal(self.journal_dir, SYMBOL)  # re-validates the hash chain
        return [e["payload"] for e in journal.events(DAY)
                if e["kind"] == "fill" and e["payload"].get("source") == "auto_recovery"]

    def test_recovery_books_its_fills_so_the_bot_restarts_the_same_day(self):
        self.bot_journal()
        broker = FakeBroker([bot_order_row()], [short("3")], liquidity=[2, 1], deal_price="108.5")
        result = self.recover(broker, journal_dir=self.journal_dir, journal_days=(DAY,))

        self.assertEqual(result["action"], "recovered")
        self.assertFalse(self.flag.exists())
        self.assertEqual(
            [(f["order_id"], f["side"], f["quantity"], f["deal_total"], f["price"]) for f in self.recovery_fills()],
            [("Dm00001", "buy", 2, 2, 108.5), ("Dm00002", "buy", 1, 1, 108.5)])
        engine = self.restart_engine()  # used to raise "journal has open position"
        self.assertEqual(engine.ledger.position, 0)
        self.assertAlmostEqual(engine.ledger.realized_pnl, (110.0 - 108.5) * 3 * 2000)

    def test_already_flat_account_repairs_the_journal_from_the_report(self):
        # The 11:47 end state: TTB flat after two recoveries, journal still -3.
        self.bot_journal()
        rows = [bot_order_row(),
                {"ORDNO": "Dm35148b", "COMD_ID": SYMBOL, "BS": "B", "ORDR_PRCE": "0", "VOLM": "3",
                 "LESS_VOLM": "0", "DEAL_TOTL": "2", "DEAL_PRCE": "108.5", "STAT": "已刪除"},
                {"ORDNO": "Dm35150c", "COMD_ID": SYMBOL, "BS": "B", "ORDR_PRCE": "0", "VOLM": "1",
                 "LESS_VOLM": "0", "DEAL_TOTL": "1", "DEAL_PRCE": "109", "STAT": "完全成交"}]
        result = self.recover(FakeBroker(rows), journal_dir=self.journal_dir, journal_days=(DAY,))

        self.assertEqual(result["action"], "already_clean")
        self.assertFalse(self.flag.exists())
        self.assertEqual([(f["order_id"], f["quantity"]) for f in self.recovery_fills()],
                         [("Dm35148b", 2), ("Dm35150c", 1)])
        self.assertEqual(self.restart_engine().ledger.position, 0)
        # Idempotent: a second run finds nothing left to book.
        self.flag.write_text("x", encoding="ascii")
        self.recover(FakeBroker(rows), journal_dir=self.journal_dir, journal_days=(DAY,))
        self.assertEqual(len(self.recovery_fills()), 2)

    def test_journal_that_disagrees_with_the_report_fails_closed_unwritten(self):
        self.bot_journal(quantity=2)  # journal -2, but TTB is short 3
        path = self.journal_dir / f"events-{SYMBOL}-{DAY}.jsonl"
        before = path.read_bytes()
        broker = FakeBroker([bot_order_row(qty="2")], [short("3")])
        with self.assertRaisesRegex(RecoveryRefused, "does not reconcile"):
            self.recover(broker, journal_dir=self.journal_dir, journal_days=(DAY,))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.flag.exists())
        self.assertEqual(broker.positions, [])  # the account itself is still flattened
        self.assertIn("auto_recovery_failed", self.audit_text())

    def test_corrupt_journal_fails_closed(self):
        self.bot_journal()
        path = self.journal_dir / f"events-{SYMBOL}-{DAY}.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"S1"', '"S9"', 1), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(RecoveryRefused, "journal unusable"):
            self.recover(FakeBroker([bot_order_row()], [short("3")]),
                         journal_dir=self.journal_dir, journal_days=(DAY,))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.flag.exists())

    def test_running_bot_blocks_recovery_before_any_order(self):
        self.bot_journal()
        broker = FakeBroker([bot_order_row()], [short("3")])
        with JournalLock(self.journal_dir):  # held by sim_hft_bot for its whole run
            with self.assertRaisesRegex(RecoveryRefused, "still holds"):
                self.recover(broker, journal_dir=self.journal_dir, journal_days=(DAY,))
        self.assertEqual(broker.market_orders, [])
        self.assertFalse(self.budget_file.exists())
        self.assertTrue(self.flag.exists())

    def test_journal_lock_is_exclusive_and_released(self):
        with JournalLock(self.journal_dir):
            with self.assertRaises(JournalError):
                JournalLock(self.journal_dir).acquire()
        JournalLock(self.journal_dir).acquire().release()

    def test_cli_reconciles_the_default_journal(self):
        self.bot_journal()
        broker = FakeBroker([bot_order_row()], [short("3")], liquidity=[2, 1])
        with patch("ttb.api.TTBApi.connect", return_value=broker), \
                patch("auto_recover_ttb.market_open", return_value=True), \
                patch("auto_recover_ttb.journal_days", return_value=(DAY,)), \
                patch("auto_recover_ttb.RecoveryPolicy",
                      lambda **bounds: RecoveryPolicy(settle_seconds=0, poll_seconds=0, timeout_seconds=0.05, **bounds)):
            code = auto_recover_main([
                "--confirm-exclusive-bot-account", "--flag", str(self.flag), "--audit-dir", str(self.audit),
                "--budget-file", str(self.budget_file), "--journal-dir", str(self.journal_dir),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.recovery_fills()), 2)
        self.assertFalse(self.flag.exists())


if __name__ == "__main__":
    unittest.main()
