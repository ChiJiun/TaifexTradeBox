from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .api import is_success_response
from .orders import _recursive_json, _value, is_terminal_order, order_number, order_remaining, order_symbol


class AccountInspectionError(RuntimeError):
    """Account data cannot be proven complete or unambiguous."""


class AccountReader(Protocol):
    def query_orders(self) -> Any: ...
    def query_positions(self) -> Any: ...
    def query_margin(self) -> Any: ...


def _rows(response: Any, endpoint: str) -> list[dict[str, Any]]:
    if not is_success_response(response):
        raise AccountInspectionError(f"{endpoint}: API did not return a success code")
    if not isinstance(response, dict):
        raise AccountInspectionError(f"{endpoint}: invalid response type")
    if "Data" not in response and "data" not in response:
        raise AccountInspectionError(f"{endpoint}: missing Data field; cannot interpret as empty")
    payload = _recursive_json(response.get("Data", response.get("data")))
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise AccountInspectionError(f"{endpoint}: Data must be a list of objects")
    return payload


def _quantity(row: dict[str, Any], keys: tuple[str, ...], *, endpoint: str) -> int:
    raw = _value(row, *keys, default=None)
    if raw is None or isinstance(raw, bool):
        raise AccountInspectionError(f"{endpoint}: missing quantity field")
    try:
        number = float(str(raw).strip().replace(",", ""))
    except (ValueError, TypeError) as exc:
        raise AccountInspectionError(f"{endpoint}: malformed quantity") from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise AccountInspectionError(f"{endpoint}: invalid nonnegative integer quantity")
    return int(number)


@dataclass
class AccountSnapshot:
    symbol: str
    checked_at: str
    flat: bool
    working_orders: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    margin_query_ok: bool = False
    margin_note: str = ""
    account_identity_verified_by_api: bool = False

    @property
    def can_clear(self) -> bool:
        return self.flat and not self.working_orders


def inspect_account(api: AccountReader, symbol: str) -> AccountSnapshot:
    """Inspect every symbol in the selected GUI account, not only the bot symbol.

    Query responses are not atomic across API calls and do not authenticate the
    currently selected TTB competition/account. Operator GUI verification is
    therefore mandatory before clearing a live failure latch.
    """
    orders = _rows(api.query_orders(), "QUERYRESTOREREPORT")
    positions = _rows(api.query_positions(), "QUERYRESTOREFILLREPORT")
    active_orders: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []

    for row in orders:
        order_no = order_number(row).strip()
        if not order_no:
            raise AccountInspectionError("order report contains a row without an order number")
        status = str(_value(row, "STAT", "Status", default="")).strip()
        remaining_raw = _value(row, "LESS_VOLM", "RemainQty", "RemainingQty", default=None)
        if remaining_raw is not None:
            remaining = _quantity(
                row, ("LESS_VOLM", "RemainQty", "RemainingQty"), endpoint="order"
            )
        else:
            total_raw = _value(row, "VOLM", "OrderQty", default=None)
            filled_raw = _value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=None)
            if total_raw is None or filled_raw is None:
                terminal_status = any(
                    token in status.lower()
                    for token in ("刪", "取消", "完全成交", "全部成交", "cancel", "filled", "rejected", "拒絕")
                )
                if terminal_status:
                    remaining = 0
                else:
                    raise AccountInspectionError("order report cannot determine remaining quantity")
            else:
                total = _quantity(row, ("VOLM", "OrderQty"), endpoint="order")
                filled = _quantity(row, ("DEAL_TOTL", "DealQty", "FilledQty"), endpoint="order")
                if filled > total:
                    raise AccountInspectionError("order report filled quantity exceeds ordered quantity")
                remaining = total - filled
        if remaining == 0 and not is_terminal_order(row):
            raise AccountInspectionError(
                "order report has zero remaining quantity without a confirmed terminal state"
            )
        if remaining and not is_terminal_order(row):
            active_orders.append({
                "order_no": order_no,
                "symbol": order_symbol(row) or "UNKNOWN",
                "remaining": remaining,
                "status": status or "UNKNOWN",
                # Ownership is unprovable after a bot crash without a durable
                # order-ID journal. Never claim bot ownership here.
                "ownership": "unverified",
            })

    for row in positions:
        hold = _quantity(row, ("Hold", "HOLD", "PositionQty", "Qty"), endpoint="position")
        if hold:
            name = str(_value(row, "ComdID1", "COMD_ID", "Symbol", default="")).strip()
            if not name:
                name = str(_value(row, "ComdID2", default="")).strip()
            open_positions.append({"symbol": name or "UNKNOWN", "quantity": hold})

    margin_ok = False
    margin_note = "not queried"
    try:
        margin_response = api.query_margin()
        margin_ok = is_success_response(margin_response)
        margin_note = "ok" if margin_ok else "margin query failed (not used to certify flatness)"
    except Exception:
        margin_note = "margin query raised an exception (not used to certify flatness)"

    return AccountSnapshot(
        symbol=symbol.upper(),
        checked_at=datetime.now(timezone.utc).isoformat(),
        flat=(len(open_positions) == 0),
        working_orders=active_orders,
        open_positions=open_positions,
        margin_query_ok=margin_ok,
        margin_note=margin_note,
    )


def append_audit(directory: Path, event: dict[str, Any]) -> Path:
    """Write sanitized, immutable per-event JSON instead of raw account reports."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"reconcile-{stamp}-{uuid.uuid4().hex[:8]}.json"
    temporary = directory / f".{target.name}.tmp"
    data = {
        "version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def audit_snapshot(snapshot: AccountSnapshot, *, action: str, reason: str = "") -> dict[str, Any]:
    return {"action": action, "reason": reason, "snapshot": asdict(snapshot)}


def clear_reconciliation_flag(
    flag: Path,
    *,
    first: AccountSnapshot,
    second: AccountSnapshot,
    audit_dir: Path,
    confirm_virtual_account: bool,
    confirm_flat_account: bool,
    confirm_no_working_orders: bool,
    confirm_bot_stopped: bool,
    expected_flag_identity: tuple[int, int, int] | None = None,
) -> Path:
    """Clear only after two complete flat-account inspections and audit.

    The flag itself is the interlock that prevents scheduled live restarts.
    Without the authorized audit on disk, the flag is never removed.
    """
    if not all((confirm_virtual_account, confirm_flat_account, confirm_no_working_orders, confirm_bot_stopped)):
        raise AccountInspectionError("all four explicit operator confirmations are required")
    if not first.can_clear or not second.can_clear:
        raise AccountInspectionError("account has open positions or working orders")
    if first.symbol != second.symbol:
        raise AccountInspectionError("inspections do not cover the same symbol")
    if flag.name != "manual-reconciliation-required.flag":
        raise AccountInspectionError("refusing to delete a file that is not the known reconciliation latch")
    if not flag.is_file():
        raise AccountInspectionError("manual reconciliation flag does not exist")
    original = flag.stat()
    current_identity = (original.st_ino, original.st_mtime_ns, original.st_size)
    if expected_flag_identity is not None and current_identity != expected_flag_identity:
        raise AccountInspectionError("reconciliation flag changed during account inspection")
    audit = append_audit(
        audit_dir,
        audit_snapshot(second, action="clear_authorized", reason="two flat inspections and four operator confirmations"),
    )
    # Detect external replacement of the latch during account inspection.
    current = flag.stat()
    if (current.st_ino, current.st_mtime_ns, current.st_size) != current_identity:
        raise AccountInspectionError("reconciliation flag changed before removal")
    flag.unlink()
    return audit
