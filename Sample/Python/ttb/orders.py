from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any, Protocol

from strategy_core import Fill, IndependentFillSimulator, OrderIntent, PositionLedger, Quote, StrategyConfig

from .api import TTBApi, TTBOrderRequest, extract_order_no, is_success_response


LOG = logging.getLogger("ttb.orders")


def _recursive_json(value: Any) -> Any:
    current = value
    for _ in range(3):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text or text[0] not in "[{\"":
            break
        try:
            current = json.loads(text)
        except json.JSONDecodeError:
            break
    return current


def _response_rows(response: Any) -> list[dict[str, Any]]:
    parsed = _recursive_json(response)
    if isinstance(parsed, dict):
        parsed = _recursive_json(parsed.get("Data", parsed.get("data", [])))
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    return []


def _value(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return default


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _side(value: Any) -> str:
    text = str(value).strip().lower()
    return "buy" if text in {"1", "b", "buy", "買", "買別", "買進"} else "sell"


def order_number(row: dict[str, Any]) -> str:
    return str(_value(row, "ORDNO", "OrdNo", "OrderNo", "order_no", default=""))


def order_symbol(row: dict[str, Any]) -> str:
    return str(_value(row, "COMD_ID", "ComdID1", "Symbol", default="")).split(".")[0].upper()


def order_remaining(row: dict[str, Any]) -> int:
    remaining = int(_float(_value(row, "LESS_VOLM", "RemainQty", "RemainingQty", default=0)))
    if remaining:
        return remaining
    ordered = int(_float(_value(row, "VOLM", "OrderQty", default=0)))
    filled = int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0)))
    return max(0, ordered - filled)


def is_terminal_order(row: dict[str, Any]) -> bool:
    status = str(_value(row, "STAT", "Status", default="")).strip().lower()
    terminal = ("刪", "取消", "完全成交", "全部成交", "cancel", "filled", "rejected", "拒絕")
    return any(token in status for token in terminal) or (
        order_remaining(row) == 0
        and int(_float(_value(row, "VOLM", "OrderQty", default=0))) > 0
    )


def is_working_order(row: dict[str, Any]) -> bool:
    return bool(order_number(row)) and order_remaining(row) > 0 and not is_terminal_order(row)


class OrderGateway(Protocol):
    ledger: PositionLedger

    def before_decision(self, quote: Quote) -> list[Fill]: ...

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


class ShadowGateway:
    """Order gateway that records simulated fills and never calls TTB orders."""

    def __init__(self, ledger: PositionLedger, simulator: IndependentFillSimulator | None = None) -> None:
        self.ledger = ledger
        self.simulator = simulator or IndependentFillSimulator()

    def before_decision(self, quote: Quote) -> list[Fill]:
        fills = self.simulator.on_quote(quote)
        for fill in fills:
            delta = self.ledger.on_fill(fill)
            LOG.info(
                "SIM FILL %s %s @ %.2f id=%s pnl_delta=%.2f position=%s realized=%.2f",
                fill.side,
                fill.quantity,
                fill.price,
                fill.order_id,
                delta,
                self.ledger.position,
                self.ledger.realized_pnl,
            )
        return fills

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None:
        self.simulator.sync(intents, quote.received_at, quote)

    def snapshot(self) -> dict[str, Any]:
        return {
            side: {"price": order.intent.price, "qty": order.remaining, "role": order.intent.role}
            for side, order in self.simulator.orders.items()
        }


class TTBOrderGateway:
    """Fail-closed TTB order gateway with one working order per side."""

    def __init__(self, module_or_api: Any, config: StrategyConfig, ledger: PositionLedger, poll_seconds: float) -> None:
        self.api = module_or_api if isinstance(module_or_api, TTBApi) else TTBApi(module_or_api)
        # Keep the old public attribute for code that inspected it.
        self.module = self.api.module
        self.config = config
        self.ledger = ledger
        self.poll_seconds = max(0.25, poll_seconds)
        self.active: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.owned_order_nos: set[str] = set()
        self.preexisting_order_nos: set[str] = set()
        self.seen_deal_qty: dict[str, int] = {}
        self.last_poll = 0.0
        self.bootstrapped = False
        self.cancel_requested: set[str] = set()

    def before_decision(self, quote: Quote) -> list[Fill]:
        if time.monotonic() - self.last_poll < self.poll_seconds:
            return []
        self.last_poll = time.monotonic()
        response = self.api.query_orders()
        if not is_success_response(response):
            raise RuntimeError("Order reconciliation failed")
        rows = _response_rows(response)
        if not self.bootstrapped:
            self.preexisting_order_nos = {
                order_number(row)
                for row in rows
                if order_number(row)
            }
            working = [
                order_number(row)
                for row in rows
                if (not order_symbol(row) or order_symbol(row) == self.config.symbol.upper())
                and is_working_order(row)
            ]
            if working:
                raise RuntimeError(
                    "Working orders already exist for this symbol; reconcile manually before startup: "
                    + ", ".join(working)
                )
            self.bootstrapped = True
            LOG.info("TTB order baseline loaded: %s existing order ids ignored", len(self.preexisting_order_nos))
            return []
        fills: list[Fill] = []
        for row in rows:
            symbol = order_symbol(row)
            if symbol and symbol != self.config.symbol.upper():
                continue
            order_no = order_number(row)
            if not order_no:
                continue
            side = _side(_value(row, "BS", "Side", "ComdBS1"))
            price = _float(_value(row, "DEAL_PRCE", "DealPrice", default=0))
            ordered_price = _float(_value(row, "ORDR_PRCE", "Price", default=0))
            ordered_qty = int(_float(_value(row, "VOLM", "OrderQty", default=0)))
            if order_no not in self.owned_order_nos:
                pending = self.pending.get(side)
                intent = pending.get("intent") if pending else None
                matches_pending = (
                    intent is not None
                    and ordered_price == intent.price
                    and (ordered_qty == 0 or ordered_qty == intent.quantity)
                    and order_no not in self.preexisting_order_nos
                )
                if not matches_pending:
                    continue
                self.owned_order_nos.add(order_no)
                self.pending.pop(side, None)
                LOG.info("TTB BIND %s to order %s", side, order_no)
            deal_total = int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0)))
            previous = self.seen_deal_qty.get(order_no, 0)
            if deal_total > previous and price > 0:
                fill = Fill(side, price, deal_total - previous, order_no, quote.received_at)
                self.ledger.on_fill(fill)
                fills.append(fill)
            self.seen_deal_qty[order_no] = max(previous, deal_total)
            remaining = order_remaining(row)
            if remaining > 0 and not is_terminal_order(row):
                self.active[side] = {"order_no": order_no, "price": ordered_price, "qty": remaining}
            elif self.active.get(side, {}).get("order_no") == order_no:
                self.active.pop(side, None)
                self.cancel_requested.discard(order_no)
        return fills

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None:
        if not self.bootstrapped:
            return
        if any(time.monotonic() - pending["created_at"] > 10 for pending in self.pending.values()):
            raise RuntimeError("Unresolved order acknowledgement; reconcile manually")
        desired = {intent.side: intent for intent in intents if intent.quantity > 0}
        # Wait for every obsolete order's terminal report before sending a
        # replacement.  This prevents duplicate working orders after latency.
        for side, current in list(self.active.items()):
            wanted = desired.get(side)
            if wanted is None or wanted.price != current["price"] or wanted.quantity != current["qty"]:
                self._cancel(side, current)
        if self.cancel_requested or self.pending:
            return
        for side in ("buy", "sell"):
            current = self.active.get(side)
            wanted = desired.get(side)
            if current and wanted is None:
                self._cancel(side, current)
            elif current and wanted and (current["price"] != wanted.price or current["qty"] != wanted.quantity):
                self._cancel(side, current)
            elif wanted and not current and side not in self.pending:
                self._place(wanted)

    def _place(self, intent: OrderIntent) -> None:
        request = TTBOrderRequest(
            symbol=self.config.symbol,
            side=intent.side,
            price=intent.price,
            quantity=intent.quantity,
            time_in_force="2" if intent.role == "risk_exit" else "1",
        )
        response = self.api.new_order(request)
        order_no = extract_order_no(response)
        if order_no:
            self.owned_order_nos.add(order_no)
            self.active[intent.side] = {
                "order_no": order_no,
                "price": intent.price,
                "qty": intent.quantity,
            }
        else:
            self.pending[intent.side] = {"intent": intent, "created_at": time.monotonic()}
        LOG.info(
            "TTB NEW %s qty=%s price=%s role=%s response=%s",
            intent.side,
            intent.quantity,
            intent.price,
            intent.role,
            response,
        )

    def _cancel(self, side: str, current: dict[str, Any]) -> None:
        order_no = current["order_no"]
        if order_no in self.cancel_requested:
            return
        self.api.cancel_order(order_no)
        LOG.info("TTB CANCEL %s %s", side, order_no)
        self.cancel_requested.add(order_no)

    def cancel_all(self) -> None:
        for side, current in list(self.active.items()):
            try:
                self._cancel(side, current)
            except Exception:
                LOG.exception("unable to cancel %s", current)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "pending": {side: asdict(value["intent"]) for side, value in self.pending.items()},
        }
