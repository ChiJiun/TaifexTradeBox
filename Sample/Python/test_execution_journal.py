from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy_core import PositionLedger, StrategyConfig
from ttb.engine import BotEngine
from ttb.journal import ExecutionJournal, JournalError
from ttb.orders import TTBOrderGateway
from ttb.runtime import RiskStateStore


class JournalIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = ExecutionJournal(self.root / "journal", "TXFA7")
        self.day = "2026-10-13"

    def test_append_reopen_verify_hash_chain_and_day_partitions(self):
        self.journal.append("order_intent", {"quantity": 1}, self.day)
        self.journal.append("order_ack", {"order_no": "A1"}, self.day)
        self.journal.append("shutdown", {"clean": True}, "2026-10-14")
        reopened = ExecutionJournal(self.root / "journal", "TXFA7")
        self.assertEqual(len(reopened.events(self.day)), 2)
        self.assertEqual(reopened.events("2026-10-14")[0]["kind"], "shutdown")
        self.assertEqual(len(list((self.root / "journal").glob("*.jsonl"))), 2)

    def test_truncated_tail_is_rejected_on_reopen(self):
        self.journal.append("order_intent", {"quantity": 1}, self.day)
        path = next((self.root / "journal").glob("*.jsonl"))
        with path.open("ab") as stream:
            stream.write(b'{"unfinished":')
        with self.assertRaisesRegex(JournalError, "truncated"):
            ExecutionJournal(self.root / "journal", "TXFA7")

    def test_modified_event_is_rejected_on_reopen(self):
        self.journal.append("order_ack", {"order_no": "A1"}, self.day)
        path = next((self.root / "journal").glob("*.jsonl"))
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace('"A1"', '"BAD"'), encoding="utf-8")
        with self.assertRaisesRegex(JournalError, "corrupt"):
            ExecutionJournal(self.root / "journal", "TXFA7")

    def test_duplicate_fill_total_is_not_replayed_twice(self):
        fill = {
            "order_id": "A1", "side": "buy", "price": 20000,
            "quantity": 1, "deal_total": 1,
        }
        self.journal.append("fill", fill, self.day)
        self.journal.append("fill", fill, self.day)
        config = StrategyConfig(symbol="TXFA7")
        with self.assertRaisesRegex(JournalError, "cannot replay fill"):
            self.journal.replay_ledger(self.day, config)


class JournalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = "2026-10-13"
        self.config = StrategyConfig(
            symbol="TXFA7", max_order_qty=1, max_inventory=1,
            fee_per_fill=10.0, daily_loss_limit=100,
        )
        self.journal = ExecutionJournal(self.root / "journal", "TXFA7")

    def _engine(self):
        class FakeModule:
            def QUERYRESTOREREPORT(self):
                return {"Code": "0000", "Data": []}
            def QUERYRESTOREFILLREPORT(self):
                return {"Code": "0000", "Data": []}

        ledger = PositionLedger(self.config)
        gateway = TTBOrderGateway(
            FakeModule(), self.config, ledger, 0.25,
            risk_counters_path=self.root / "risk-counters.json",
            journal_dir=self.root / "journal",
        )
        engine = BotEngine(
            self.config, gateway, self.root / "run",
            risk_state_path=self.root / "risk-state.json",
        )
        self.addCleanup(engine.recorder.close)
        self.addCleanup(engine.raw_recorder.close)
        return engine

    def _restore(self, engine):
        engine._restore_or_roll_risk_state(
            datetime(2026, 10, 13, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        )

    def test_crash_after_journal_fill_before_checkpoint_recovers_realized_pnl(self):
        # Snapshot written before the two actual fills reached disk.
        RiskStateStore(self.root / "risk-state.json", "TXFA7").checkpoint(
            trading_day=self.day, realized_pnl=0, peak_realized_pnl=0,
            fees_and_tax=0, fill_count=0, peak_equity=0,
            halt_reason="", position=0,
        )
        self.journal.append("fill", {
            "order_id": "BUY", "side": "buy", "price": 20000,
            "quantity": 1, "deal_total": 1,
        }, self.day)
        self.journal.append("fill", {
            "order_id": "SELL", "side": "sell", "price": 20003,
            "quantity": 1, "deal_total": 1,
        }, self.day)
        engine = self._engine()
        self._restore(engine)
        self.assertEqual(engine.ledger.position, 0)
        self.assertEqual(engine.ledger.fill_count, 2)
        self.assertEqual(engine.ledger.fees_and_tax, 20)
        self.assertEqual(engine.ledger.realized_pnl, 580)
        self.assertEqual(engine.gateway.seen_deal_qty, {"BUY": 1, "SELL": 1})

    def test_crash_with_open_fill_requires_operator_reconciliation(self):
        self.journal.append("fill", {
            "order_id": "BUY", "side": "buy", "price": 20000,
            "quantity": 1, "deal_total": 1,
        }, self.day)
        engine = self._engine()
        with self.assertRaisesRegex(RuntimeError, "open position"):
            self._restore(engine)

    def test_checkpoint_with_unjournaled_fills_is_rejected(self):
        RiskStateStore(self.root / "risk-state.json", "TXFA7").checkpoint(
            trading_day=self.day, realized_pnl=1, peak_realized_pnl=1,
            fees_and_tax=0, fill_count=3, peak_equity=0,
            halt_reason="", position=0,
        )
        engine = self._engine()
        with self.assertRaisesRegex(RuntimeError, "absent from journal"):
            self._restore(engine)

    def test_corrupt_risk_snapshot_is_not_overwritten_during_shutdown(self):
        path = self.root / "risk-state.json"
        path.write_text("{corrupt", encoding="utf-8")
        engine = self._engine()
        with self.assertRaisesRegex(RuntimeError, "unreadable"):
            self._restore(engine)
        engine.close()
        self.assertEqual(path.read_text(encoding="utf-8"), "{corrupt")

    def test_loss_halt_recovers_even_if_checkpoint_not_updated(self):
        for kind, side, price in (
            ("BUY", "buy", 20000), ("SELL", "sell", 19998),
        ):
            self.journal.append("fill", {
                "order_id": kind, "side": side, "price": price,
                "quantity": 1, "deal_total": 1,
            }, self.day)
        engine = self._engine()
        self._restore(engine)
        self.assertLess(engine.ledger.realized_pnl, -self.config.daily_loss_limit)
        self.assertEqual(engine.strategy.halt_reason, "journal_recovered_daily_loss_limit")


if __name__ == "__main__":
    unittest.main()
