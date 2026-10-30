from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from strategy_core import StrategyConfig

from .api import extract_order_no
from .calendar import CONTRACT_CODE, SUPPORTED_PRODUCTS
from .journal import ExecutionJournal, JournalError, JournalLock
from .orders import (
    _explicit_side, _float, _response_rows, _strict_quantity, _value,
    is_working_order, order_number, order_symbol, reported_position,
)
from .reconciliation import AccountInspectionError, AccountSnapshot, append_audit, inspect_account
from .runtime import TAIPEI_TZ


FLAG_NAME = "manual-reconciliation-required.flag"


class RecoveryRefused(RuntimeError):
    """Automatic recovery is not allowed; an operator must reconcile."""


class RecoveryDeferred(RuntimeError):
    """Recovery cannot finish now (market closed, TTB logged out); retry later."""


@dataclass(frozen=True)
class RecoveryPolicy:
    """Bounds for unattended recovery of an account that only this bot trades."""

    max_attempts_per_day: int = 3
    max_flatten_qty: int = 10
    # Market IOCs per symbol: thin stock-futures books fill only part of one.
    max_flatten_rounds: int = 3
    settle_seconds: float = 3.0
    timeout_seconds: float = 45.0
    poll_seconds: float = 1.0

    def validate(self) -> None:
        if self.max_attempts_per_day <= 0 or self.max_flatten_qty <= 0 or self.max_flatten_rounds <= 0:
            raise ValueError("recovery attempt, quantity and round bounds must be positive")


class AttemptBudget:
    """Durable per-Taipei-day count of recoveries that sent cancels/orders."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit

    def _load(self, today: str) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRefused("recovery budget file unreadable") from exc
        if not isinstance(data, dict) or not isinstance(data.get("attempts"), int):
            raise RecoveryRefused("recovery budget file invalid")
        return data["attempts"] if data.get("date") == today else 0

    def consume(self, today: str) -> int:
        used = self._load(today)
        if used >= self.limit:
            raise RecoveryRefused(f"automatic recovery budget exhausted ({used}/{self.limit} today)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"date": today, "attempts": used + 1}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        return used + 1


def _signed_positions(api: Any) -> dict[str, int]:
    response = api.query_positions()
    rows = _response_rows(response)
    symbols: set[str] = set()
    for row in rows:
        for name in (_value(row, "ComdID1", "COMD_ID", "Symbol", default=""), _value(row, "ComdID2", default="")):
            code = str(name).split(".")[0].upper()
            if code:
                symbols.add(code)
    return {symbol: qty for symbol in sorted(symbols) if (qty := reported_position(response, symbol))}


def _working_orders(api: Any) -> list[tuple[str, str]]:
    response = api.query_orders()
    return [(order_number(row), order_symbol(row)) for row in _response_rows(response) if is_working_order(row)]


def _wait(condition: Callable[[], Any], policy: RecoveryPolicy, sleep: Callable[[float], None]) -> Any:
    """First truthy value of condition, or None at the timeout."""
    deadline = time.monotonic() + policy.timeout_seconds
    while True:
        try:
            value = condition()
            if value:
                return value
        except RecoveryRefused:
            raise
        except (RuntimeError, AccountInspectionError):
            pass  # report briefly inconsistent; keep polling until timeout
        if time.monotonic() >= deadline:
            return None
        sleep(policy.poll_seconds)


def _final_flatten_row(
    api: Any, known: set[str], order_no: str, symbol: str, side: str, quantity: int,
) -> dict[str, Any] | None:
    """The report row of the market IOC just sent, once it can no longer fill."""
    rows = _response_rows(api.query_orders())
    if order_no:
        matches = [row for row in rows if order_number(row) == order_no]
    else:
        # As in the bot, NEWORDER may not echo OrdNo; the account is
        # exclusive and the bot is stopped, so one new matching row is ours.
        matches = [
            row for row in rows
            if order_number(row) and order_number(row) not in known
            and order_symbol(row) in ("", symbol)
            and _explicit_side(_value(row, "BS", "Side", "ComdBS1")) == side
            and int(_float(_value(row, "VOLM", "OrderQty", default=0))) == quantity
        ]
    if len({order_number(row) for row in matches}) > 1:
        raise RecoveryRefused(f"several TTB orders match one flatten order for {symbol}")
    if not matches or is_working_order(matches[0]):
        return None
    return matches[0]


def _flatten_symbol(
    api: Any, symbol: str, quantity: int, policy: RecoveryPolicy,
    sleep: Callable[[float], None], actions: list[dict[str, Any]],
) -> None:
    """Market IOC until flat, resending only the rest the report proves open."""
    remaining = quantity
    for round_no in range(1, policy.max_flatten_rounds + 1):
        side = "sell" if remaining > 0 else "buy"
        size = abs(remaining)
        known = {order_number(row) for row in _response_rows(api.query_orders()) if order_number(row)}
        response = api.new_market_ioc(symbol, side, size)
        action: dict[str, Any] = {"flatten": symbol, "side": side, "quantity": size, "round": round_no}
        actions.append(action)
        row = _wait(lambda: _final_flatten_row(api, known, extract_order_no(response), symbol, side, size),
                    policy, sleep)
        if row is None:
            raise RecoveryRefused(f"flatten order for {symbol} never reached a final state in the TTB report")
        filled = _strict_quantity(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0), "filled quantity")
        if filled > size:
            raise RecoveryRefused(f"flatten order for {symbol} reports more fills than ordered")
        action.update(order_no=order_number(row), filled=filled)
        remaining += filled if side == "buy" else -filled
        # The position report must agree with the fills before any resend;
        # a lagging report would otherwise resend lots that already closed.
        expected = remaining
        if not _wait(lambda: _signed_positions(api).get(symbol, 0) == expected or None, policy, sleep):
            raise RecoveryRefused(f"{symbol} position report disagrees with flatten fills (expected {expected})")
        if remaining == 0:
            return
        sleep(policy.settle_seconds)
    raise RecoveryRefused(
        f"account still not flat after {policy.max_flatten_rounds} market IOC rounds ({symbol} {remaining})")


def _clean_twice(api: Any, policy: RecoveryPolicy, sleep: Callable[[float], None]) -> AccountSnapshot | None:
    first = inspect_account(api, "ACCOUNT")
    if not first.can_clear:
        return None
    sleep(policy.settle_seconds)
    second = inspect_account(api, "ACCOUNT")
    return second if second.can_clear else None


def _journal_symbols(journal_dir: Path, days: tuple[str, ...]) -> set[str]:
    symbols: set[str] = set()
    for day in days:
        suffix = f"-{day}.jsonl"
        for path in journal_dir.glob(f"events-*{suffix}"):
            symbols.add(path.name[len("events-"):-len(suffix)].upper())
    return symbols


def _plan_journal_fills(
    api: Any, journal_dir: Path, days: tuple[str, ...],
) -> list[tuple[ExecutionJournal, str, dict[str, Any]]]:
    """Fill events that bring each journal in line with the flat account.

    Runs only after two clean inspections, so every TTB order is final and
    every position is 0. Fills the journal lacks (auto-recovery's own market
    IOCs, or fills of bot orders after the bot stopped) are taken from the
    order report; an order already journaled on another day is left alone.
    The plan is accepted only if replaying it leaves the journal of every
    symbol flat; anything else refuses without writing.
    """
    filled: dict[str, list[dict[str, Any]]] = {}
    for row in _response_rows(api.query_orders()):
        if not _strict_quantity(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0), "filled quantity"):
            continue
        symbol = order_symbol(row)
        if not symbol or not order_number(row):
            raise RecoveryRefused("a filled TTB order has no symbol or order number; reconcile the journal manually")
        filled.setdefault(symbol, []).append(row)

    plan: list[tuple[ExecutionJournal, str, dict[str, Any]]] = []
    for symbol in sorted(set(filled) | _journal_symbols(journal_dir, days)):
        journal = ExecutionJournal(journal_dir, symbol)  # validates every retained partition
        partitions = journal.trading_days()
        targets = [day for day in days if day in partitions]
        if len(targets) > 1:
            raise RecoveryRefused(f"{symbol} has journals for several candidate trading days {targets}")
        target = targets[0] if targets else ""
        elsewhere = {
            str(event["payload"].get("order_no") or event["payload"].get("order_id") or "")
            for day in partitions if day != target for event in journal.events(day)
        }
        position, cursors, acked = 0, {}, {}
        if target:
            ledger, cursors = journal.replay_ledger(target, StrategyConfig(symbol=symbol))
            position = ledger.position
            acked = {str(event["payload"].get("order_no")): str(event["payload"].get("side"))
                     for event in journal.events(target) if event["kind"] == "order_ack"}
        additions: list[dict[str, Any]] = []
        for row in filled.get(symbol, []):
            order_no = order_number(row)
            if order_no in elsewhere and order_no not in cursors and order_no not in acked:
                continue
            total = _strict_quantity(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0), "filled quantity")
            previous = cursors.get(order_no, 0)
            if total < previous:
                raise RecoveryRefused(f"journal holds more fills than TTB for {symbol} order {order_no}")
            if total == previous:
                continue
            side = _explicit_side(_value(row, "BS", "Side", "ComdBS1"))
            price = _float(_value(row, "DEAL_PRCE", "DealPrice", default=0))
            if not side or price <= 0 or acked.get(order_no, side) != side:
                raise RecoveryRefused(f"TTB fill of {symbol} order {order_no} has no usable side/price")
            delta = total - previous
            additions.append({
                "order_id": order_no, "side": side, "price": price, "quantity": delta,
                "deal_total": total, "source": "auto_recovery",
            })
            position += delta if side == "buy" else -delta
        if position != 0:
            raise RecoveryRefused(
                f"{symbol} journal ({target or 'no partition'}) does not reconcile with the TTB report "
                f"(position {position} after {len(additions)} report fills); reconcile manually")
        if additions and not target:
            raise RecoveryRefused(f"{symbol} has unjournaled fills but no journal for trading days {list(days)}")
        plan.extend((journal, target, payload) for payload in additions)
    return plan


def _reconcile_journals(
    api: Any, journal_dir: Path, days: tuple[str, ...], audit_dir: Path, actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Write the planned fills, then prove every journal replays flat."""
    try:
        plan = _plan_journal_fills(api, journal_dir, days)
        written: list[dict[str, Any]] = []
        for journal, day, payload in plan:
            journal.append("fill", payload, trading_day=day)
            written.append({"journal_fill": journal.symbol, "trading_day": day, **payload})
        for symbol in sorted({journal.symbol for journal, _day, _payload in plan}):
            reread = ExecutionJournal(journal_dir, symbol)
            if any(reread.replay_ledger(day, StrategyConfig(symbol=symbol))[0].position for day in days):
                raise RecoveryRefused(f"{symbol} journal still not flat after reconciliation")
    except (JournalError, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, RecoveryRefused) else f"execution journal unusable: {exc}"
        append_audit(audit_dir, {"action": "auto_recovery_failed", "reason": reason, "actions": actions})
        raise RecoveryRefused(reason) from exc
    return written


def _clear_flag(flag: Path, audit_dir: Path, snapshot: AccountSnapshot, actions: list[dict[str, Any]]) -> bool:
    if flag.name != FLAG_NAME:
        raise RecoveryRefused("refusing to delete a file that is not the reconciliation latch")
    append_audit(audit_dir, {
        "action": "auto_recovery_clear", "actions": actions, "snapshot": asdict(snapshot),
    })
    existed = flag.is_file()
    flag.unlink(missing_ok=True)
    return existed


def auto_recover(
    api: Any,
    *,
    flag: Path,
    audit_dir: Path,
    budget: AttemptBudget,
    policy: RecoveryPolicy = RecoveryPolicy(),
    market_open: bool,
    sleep: Callable[[float], None] = time.sleep,
    today: str | None = None,
    journal_dir: Path | None = None,
    journal_days: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Cancel every working order and flatten futures, then clear the latch.

    Preconditions enforced by the caller (CLI/supervisor): the TTB account is
    used exclusively by this bot and the bot process is stopped. Every write
    is audited; the latch is cleared only after two clean inspections.

    With journal_dir, the bot's journal lock must be free (bot stopped), and
    the latch is cleared only once the journals of journal_days (the trading
    days a restarted bot may replay; default today) match the flat account.
    """
    policy.validate()
    day = today or datetime.now(TAIPEI_TZ).date().isoformat()
    lock = JournalLock(journal_dir) if journal_dir is not None else None
    if lock is not None:
        try:
            lock.acquire()
        except JournalError as exc:
            raise RecoveryRefused(f"the bot still holds its execution journal; refusing to recover: {exc}") from exc
    try:
        return _recover(api, flag, audit_dir, budget, policy, market_open, sleep, day,
                        journal_dir, tuple(dict.fromkeys(journal_days or (day,))))
    finally:
        if lock is not None:
            lock.release()


def _recover(
    api: Any, flag: Path, audit_dir: Path, budget: AttemptBudget, policy: RecoveryPolicy,
    market_open: bool, sleep: Callable[[float], None], day: str,
    journal_dir: Path | None, journal_days: tuple[str, ...],
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    try:
        clean = _clean_twice(api, policy, sleep)
    except AccountInspectionError as exc:
        raise RecoveryDeferred(f"account reports unavailable: {exc}") from exc
    if clean is not None:
        if journal_dir is not None:
            actions += _reconcile_journals(api, journal_dir, journal_days, audit_dir, actions)
        cleared = _clear_flag(flag, audit_dir, clean, actions)
        return {"ok": True, "action": "already_clean", "actions": actions, "flag_cleared": cleared}

    positions = _signed_positions(api)
    unsupported = sorted(symbol for symbol in positions if not CONTRACT_CODE.fullmatch(symbol))
    if unsupported:
        raise RecoveryRefused(f"positions outside {'/'.join(SUPPORTED_PRODUCTS)} futures need manual handling: {unsupported}")
    if any(abs(qty) > policy.max_flatten_qty for qty in positions.values()):
        raise RecoveryRefused(f"position larger than max_flatten_qty={policy.max_flatten_qty}: {positions}")
    if positions and not market_open:
        raise RecoveryDeferred(f"open positions {positions} wait for the next trading session")

    attempt = budget.consume(day)
    append_audit(audit_dir, {"action": "auto_recovery_start", "attempt": attempt, "positions": positions})

    for order_no, symbol in _working_orders(api):
        try:
            api.cancel_order(order_no)
            actions.append({"cancel": order_no, "symbol": symbol})
        except Exception as exc:  # may have filled/cancelled meanwhile; re-inspection decides
            actions.append({"cancel": order_no, "symbol": symbol, "error": type(exc).__name__})
    if not _wait(lambda: not _working_orders(api), policy, sleep):
        append_audit(audit_dir, {"action": "auto_recovery_failed", "reason": "working orders remain", "actions": actions})
        raise RecoveryRefused("working orders still present after cancel requests")

    positions = _signed_positions(api)
    for symbol, qty in positions.items():
        try:
            _flatten_symbol(api, symbol, qty, policy, sleep, actions)
        except RecoveryRefused as exc:
            append_audit(audit_dir, {"action": "auto_recovery_failed", "reason": str(exc), "actions": actions})
            raise

    clean = _clean_twice(api, policy, sleep)
    if clean is None:
        append_audit(audit_dir, {"action": "auto_recovery_failed", "reason": "final inspection", "actions": actions})
        raise RecoveryRefused("final double inspection is not clean")
    if journal_dir is not None:
        actions += _reconcile_journals(api, journal_dir, journal_days, audit_dir, actions)
    cleared = _clear_flag(flag, audit_dir, clean, actions)
    return {"ok": True, "action": "recovered", "attempt": attempt, "actions": actions, "flag_cleared": cleared}
