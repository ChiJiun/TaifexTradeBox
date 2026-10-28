import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from ttb.journal import ExecutionJournal
from ttb.trade_db import main, sync_journal


class TradeDbTests(unittest.TestCase):
    def test_fills_and_orders_are_queryable_and_ingest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_dir, db = Path(tmp) / "journal", Path(tmp) / "trades.sqlite3"
            journal = ExecutionJournal(journal_dir, "TMFK6")
            journal.append("order_ack", {"order_no": "Dm1", "side": "buy", "quantity": 1,
                                         "price": 45080.0, "role": "entry", "source": "report"}, "2026-10-29")
            journal.append("fill", {"order_id": "Dm1", "side": "buy", "price": 45080.0,
                                    "quantity": 1, "deal_total": 1}, "2026-10-29")
            self.assertEqual(sync_journal(journal_dir, db), 2)
            self.assertEqual(sync_journal(journal_dir, db), 0)
            journal.append("fill", {"order_id": "Dm2", "side": "sell", "price": 45082.0,
                                    "quantity": 1, "deal_total": 1}, "2026-10-29")
            self.assertEqual(main(["--journal-dir", str(journal_dir), "--db", str(db)]), 0)
            with closing(sqlite3.connect(db)) as conn:
                fills = conn.execute("SELECT order_no, side, price FROM fills ORDER BY order_no").fetchall()
                orders = conn.execute("SELECT order_no, source FROM orders").fetchall()
            self.assertEqual(fills, [("Dm1", "buy", 45080.0), ("Dm2", "sell", 45082.0)])
            self.assertEqual(orders, [("Dm1", "report")])


if __name__ == "__main__":
    unittest.main()
