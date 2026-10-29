from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .calendar import CONTRACT_CODE, SUPPORTED_PRODUCTS
from .orders import _response_rows, is_working_order, order_number, order_symbol, reported_position, _value
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
    settle_seconds: float = 3.0
    timeout_seconds: float = 45.0
    poll_seconds: float = 1.0

    def validate(self) -> None:
        if self.max_attempts_per_day <= 0 or self.max_flatten_qty <= 0:
            raise ValueError("recovery attempt and quantity bounds must be positive")


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


def _wait(condition: Callable[[], bool], policy: RecoveryPolicy, sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + policy.timeout_seconds
    while True:
        try:
            if condition():
                return True
        except (RuntimeError, AccountInspectionError):
            pass  # report briefly inconsistent; keep polling until timeout
        if time.monotonic() >= deadline:
            return False
        sleep(policy.poll_seconds)


def _clean_twice(api: Any, policy: RecoveryPolicy, sleep: Callable[[float], None]) -> AccountSnapshot | None:
    first = inspect_account(api, "ACCOUNT")
    if not first.can_clear:
        return None
    sleep(policy.settle_seconds)
    second = inspect_account(api, "ACCOUNT")
    return second if second.can_clear else None


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
) -> dict[str, Any]:
    """Cancel every working order and flatten futures, then clear the latch.

    Preconditions enforced by the caller (CLI/supervisor): the TTB account is
    used exclusively by this bot and the bot process is stopped. Every write
    is audited; the latch is cleared only after two clean inspections.
    """
    policy.validate()
    day = today or datetime.now(TAIPEI_TZ).date().isoformat()
    actions: list[dict[str, Any]] = []
    try:
        clean = _clean_twice(api, policy, sleep)
    except AccountInspectionError as exc:
        raise RecoveryDeferred(f"account reports unavailable: {exc}") from exc
    if clean is not None:
        cleared = _clear_flag(flag, audit_dir, clean, actions)
        return {"ok": True, "action": "already_clean", "flag_cleared": cleared}

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
        side = "sell" if qty > 0 else "buy"
        api.new_market_ioc(symbol, side, abs(qty))
        actions.append({"flatten": symbol, "side": side, "quantity": abs(qty)})
    if positions and not _wait(lambda: not _signed_positions(api) and not _working_orders(api), policy, sleep):
        append_audit(audit_dir, {"action": "auto_recovery_failed", "reason": "position not flat", "actions": actions})
        raise RecoveryRefused("account still not flat after market IOC flatten")

    clean = _clean_twice(api, policy, sleep)
    if clean is None:
        append_audit(audit_dir, {"action": "auto_recovery_failed", "reason": "final inspection", "actions": actions})
        raise RecoveryRefused("final double inspection is not clean")
    cleared = _clear_flag(flag, audit_dir, clean, actions)
    return {"ok": True, "action": "recovered", "attempt": attempt, "actions": actions, "flag_cleared": cleared}
