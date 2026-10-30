from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from strategy_core import Fill, PositionLedger, StrategyConfig


class JournalError(RuntimeError):
    """A corrupted/unwritable execution journal must stop all live trading."""


class JournalLock:
    """Exclusive OS lock on a journal directory, held by the live bot for its whole run.

    The OS releases it when the holder exits, even on a crash, so a lock that
    cannot be taken proves a live process (the bot) still owns the journals.
    Offline writers such as auto-recovery take it before touching them.
    """

    NAME = "writer.lock"

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / self.NAME
        self._handle: Any = None

    def acquire(self) -> "JournalLock":
        if self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise JournalError(f"execution journal is locked by a running process: {self.path}") from exc
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # closing the handle drops the lock anyway
        finally:
            handle.close()

    def __enter__(self) -> "JournalLock":
        return self.acquire()

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class ExecutionJournal:
    """Append-only, fsync-backed, hash-chained JSONL partitioned by trading day.

    Journal files are never automatically truncated or removed. Each append
    writes one complete JSONL record before any associated broker API side
    effect or ledger mutation. Partial/tampered tail records fail on reopening.
    """

    VERSION = 1

    def __init__(self, directory: Path, symbol: str, validate_all: bool = True) -> None:
        self.directory = Path(directory)
        self.symbol = symbol.upper()
        if not re.fullmatch(r"[A-Z0-9_.-]+", self.symbol):
            raise ValueError("invalid symbol for journal filename")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, list[dict[str, Any]]] = {}
        if validate_all:
            # Validate every retained partition on startup, not just today.
            for date_text in self.trading_days():
                self._read(date_text)

    def trading_days(self) -> list[str]:
        """Trading days that have a partition on disk for this symbol."""
        return ["-".join(path.stem.rsplit("-", 3)[-3:])
                for path in sorted(self.directory.glob(f"events-{self.symbol}-*.jsonl"))]

    def _path(self, trading_day: str) -> Path:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trading_day):
            raise JournalError("trading_day must be YYYY-MM-DD")
        try:
            datetime.strptime(trading_day, "%Y-%m-%d")
        except ValueError as exc:
            raise JournalError("invalid trading_day") from exc
        return self.directory / f"events-{self.symbol}-{trading_day}.jsonl"

    def _read(self, trading_day: str) -> list[dict[str, Any]]:
        if trading_day in self._cache:
            return self._cache[trading_day]
        path = self._path(trading_day)
        records: list[dict[str, Any]] = []
        prev = "0" * 64
        try:
            with path.open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        raise JournalError(f"{path.name}: truncated journal tail")
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("not an object")
                        check = {key: record[key] for key in (
                            "version", "symbol", "trading_day", "seq",
                            "previous_hash", "recorded_at", "kind", "payload",
                        )}
                        encoded = json.dumps(
                            check, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                        expected = hashlib.sha256(encoded).hexdigest()
                        if (
                            record.get("hash") != expected
                            or check["version"] != self.VERSION
                            or check["symbol"] != self.symbol
                            or check["trading_day"] != trading_day
                            or check["seq"] != len(records) + 1
                            or check["previous_hash"] != prev
                            or not isinstance(check["kind"], str)
                            or not isinstance(check["payload"], dict)
                        ):
                            raise ValueError("invalid journal chain or schema")
                    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
                        raise JournalError(f"{path.name}: corrupt execution journal") from exc
                    prev = record["hash"]
                    records.append(record)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise JournalError(f"journal cannot be read: {path}") from exc
        self._cache[trading_day] = records
        return records

    @staticmethod
    def local_day() -> str:
        return datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    def events(self, trading_day: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read(trading_day))

    def append(self, kind: str, payload: dict[str, Any], trading_day: str | None = None) -> dict[str, Any]:
        day = trading_day or self.local_day()
        path = self._path(day)
        if not kind.strip() or not isinstance(payload, dict):
            raise JournalError("invalid journal event")
        with self._lock:
            entries = self._read(day)
            record = {
                "version": self.VERSION,
                "symbol": self.symbol,
                "trading_day": day,
                "seq": len(entries) + 1,
                "previous_hash": entries[-1]["hash"] if entries else "0" * 64,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "payload": payload,
            }
            encoded = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            record["hash"] = hashlib.sha256(encoded).hexdigest()
            line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
            try:
                with path.open("ab", buffering=0) as output:
                    output.write(line)
                    os.fsync(output.fileno())
            except OSError as exc:
                raise JournalError(f"journal write failed; trading must stop: {path}") from exc
            entries.append(record)
            return record

    def replay_ledger(self, trading_day: str, config: StrategyConfig) -> tuple[PositionLedger, dict[str, int]]:
        return replay_fills(
            (event["payload"] for event in self.events(trading_day) if event["kind"] == "fill"), config,
        )


def replay_fills(fills: Iterable[dict[str, Any]], config: StrategyConfig) -> tuple[PositionLedger, dict[str, int]]:
    """Replay fill payloads with the journal's cursor rules; also vets fills before they are appended."""
    ledger = PositionLedger(config)
    filled_totals: dict[str, int] = {}
    for row in fills:
        try:
            order_id = str(row["order_id"])
            total = int(row["deal_total"])
            delta = int(row["quantity"])
            side = str(row["side"])
            price = float(row["price"])
            if (
                not order_id or side not in ("buy", "sell") or delta <= 0 or
                total <= filled_totals.get(order_id, 0) or
                total - filled_totals.get(order_id, 0) != delta or price <= 0
            ):
                raise ValueError("invalid fill cursor")
            ledger.on_fill(Fill(side, price, delta, order_id))
            filled_totals[order_id] = total
        except (KeyError, ValueError, TypeError) as exc:
            raise JournalError("cannot replay fill; execution journal corrupt") from exc
    return ledger, filled_totals
