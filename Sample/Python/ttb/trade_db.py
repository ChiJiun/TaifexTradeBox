"""Mirror the hash-chained execution journal into a local SQLite database.

The JSONL journal stays the authority; this copy exists for queries. Ingest
is idempotent (keyed by symbol, trading day and sequence), so the supervisor
can run it after every bot exit and again on the next boot.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
import sys
from pathlib import Path

from .journal import ExecutionJournal

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    symbol TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    seq INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (symbol, trading_day, seq)
);
CREATE VIEW IF NOT EXISTS fills AS
SELECT symbol, trading_day, recorded_at,
       json_extract(payload, '$.order_id') AS order_no,
       json_extract(payload, '$.side') AS side,
       json_extract(payload, '$.price') AS price,
       json_extract(payload, '$.quantity') AS quantity
FROM events WHERE kind = 'fill';
CREATE VIEW IF NOT EXISTS orders AS
SELECT symbol, trading_day, recorded_at,
       json_extract(payload, '$.order_no') AS order_no,
       json_extract(payload, '$.side') AS side,
       json_extract(payload, '$.price') AS price,
       json_extract(payload, '$.quantity') AS quantity,
       json_extract(payload, '$.role') AS role,
       json_extract(payload, '$.source') AS source
FROM events WHERE kind = 'order_ack';
"""


def sync_journal(journal_dir: Path, db_path: Path) -> tuple[int, list[str]]:
    """Copy every journal record not yet in the database.

    Returns how many were added and the partitions that could not be read;
    one corrupt day must not keep every other day out of the database.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    errors: list[str] = []
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(SCHEMA)
        journals: dict[str, ExecutionJournal] = {}
        for path in sorted(Path(journal_dir).glob("events-*-*.jsonl")):
            symbol, *date_parts = path.stem[len("events-"):].rsplit("-", 3)
            try:
                if symbol not in journals:
                    journals[symbol] = ExecutionJournal(path.parent, symbol, validate_all=False)
                records = journals[symbol].events("-".join(date_parts))
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            for record in records:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record["symbol"], record["trading_day"], record["seq"], record["recorded_at"],
                     record["kind"], json.dumps(record["payload"], ensure_ascii=False), record["hash"]),
                )
                added += cursor.rowcount
    return added, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy the execution journal into a local SQLite database")
    parser.add_argument("--journal-dir", type=Path, default=Path("output/journal"))
    parser.add_argument("--db", type=Path, default=Path("output/trades.sqlite3"))
    args = parser.parse_args(argv)
    try:
        added, errors = sync_journal(args.journal_dir, args.db)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": not errors, "added": added, "db": str(args.db), "errors": errors},
                     ensure_ascii=False))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
