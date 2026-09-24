from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from strategy_core import Quote

from .api import TTBApi, TTBOrderRequest, extract_order_no, is_success_response
from .orders import (
    _float,
    _response_rows,
    _side,
    _value,
    is_terminal_order,
    is_working_order,
    order_number,
    order_symbol,
)


@dataclass
class OrderProbeResult:
    symbol: str
    side: str
    reference_price: float
    limit_price: float
    order_no: str = ""
    order_accepted: bool = False
    cancel_accepted: bool = False
    cancel_confirmed: bool = False
    unexpected_fill_quantity: int = 0
    message: str = ""


def far_limit_price(quote: Quote, side: str, tick_size: float, distance_ticks: int) -> float:
    if tick_size <= 0 or distance_ticks <= 0:
        raise ValueError("tick_size and distance_ticks must be positive")
    reference = quote.bid if side == "buy" else quote.ask
    shifted = reference - distance_ticks * tick_size if side == "buy" else reference + distance_ticks * tick_size
    if shifted <= 0:
        raise ValueError("probe price would be non-positive; reduce distance_ticks")
    units = shifted / tick_size
    rounded = math.floor(units) if side == "buy" else math.ceil(units)
    return round(rounded * tick_size, 10)


def _matching_order_no(
    response: Any,
    *,
    baseline: set[str],
    symbol: str,
    side: str,
    price: float,
    quantity: int = 1,
) -> str:
    for row in _response_rows(response):
        number = order_number(row)
        if not number or number in baseline:
            continue
        row_symbol = order_symbol(row)
        if row_symbol and row_symbol != symbol.upper():
            continue
        if _side(_value(row, "BS", "Side", "ComdBS1")) != side:
            continue
        row_price = _float(_value(row, "ORDR_PRCE", "Price", default=0))
        row_qty = int(_float(_value(row, "VOLM", "OrderQty", default=0)))
        if math.isclose(row_price, price, abs_tol=1e-9) and row_qty in (0, quantity):
            return number
    return ""


class OrderProbe:
    """Submit one remote limit order and cancel it as soon as it is acknowledged."""

    def __init__(
        self,
        api: TTBApi,
        symbol: str,
        side: str = "buy",
        tick_size: float = 1.0,
        distance_ticks: int = 2_000,
        ack_timeout: float = 10.0,
        cancel_timeout: float = 10.0,
        poll_seconds: float = 0.25,
    ) -> None:
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        self.api = api
        self.symbol = symbol.upper()
        self.side = side
        self.tick_size = tick_size
        self.distance_ticks = distance_ticks
        self.ack_timeout = ack_timeout
        self.cancel_timeout = cancel_timeout
        self.poll_seconds = max(0.1, poll_seconds)

    def run(self, quote: Quote) -> OrderProbeResult:
        baseline_response = self.api.query_orders()
        if not is_success_response(baseline_response):
            raise RuntimeError(f"unable to query existing orders: {baseline_response}")
        baseline_rows = _response_rows(baseline_response)
        working = [
            order_number(row)
            for row in baseline_rows
            if (not order_symbol(row) or order_symbol(row) == self.symbol) and is_working_order(row)
        ]
        if working:
            raise RuntimeError("working orders already exist; probe refused: " + ", ".join(working))
        baseline = {order_number(row) for row in baseline_rows if order_number(row)}
        price = far_limit_price(quote, self.side, self.tick_size, self.distance_ticks)
        result = OrderProbeResult(
            symbol=self.symbol,
            side=self.side,
            reference_price=quote.bid if self.side == "buy" else quote.ask,
            limit_price=price,
        )
        order_no = ""
        try:
            response = self.api.new_order(
                TTBOrderRequest(self.symbol, self.side, price, quantity=1, time_in_force="1")
            )
            result.order_accepted = True
            order_no = extract_order_no(response)
            deadline = time.monotonic() + self.ack_timeout
            while not order_no and time.monotonic() < deadline:
                report = self.api.query_orders()
                if is_success_response(report):
                    order_no = _matching_order_no(
                        report,
                        baseline=baseline,
                        symbol=self.symbol,
                        side=self.side,
                        price=price,
                    )
                if not order_no:
                    time.sleep(self.poll_seconds)
            if not order_no:
                raise RuntimeError("order was accepted but no order number appeared; reconcile manually")
            result.order_no = order_no
            self.api.cancel_order(order_no)
            result.cancel_accepted = True
            deadline = time.monotonic() + self.cancel_timeout
            while time.monotonic() < deadline:
                report = self.api.query_orders()
                rows = [row for row in _response_rows(report) if order_number(row) == order_no]
                if rows and all(is_terminal_order(row) for row in rows):
                    result.cancel_confirmed = True
                    break
                time.sleep(self.poll_seconds)
            fills = self.api.query_fills()
            for row in _response_rows(fills):
                if order_number(row) == order_no:
                    result.unexpected_fill_quantity += int(
                        _float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", "VOLM", default=0))
                    )
            if result.unexpected_fill_quantity:
                result.message = "UNEXPECTED FILL: manually reconcile and flatten the virtual account"
            elif result.cancel_confirmed:
                result.message = "order accepted and cancellation confirmed"
            else:
                result.message = "cancel accepted but terminal order report was not observed before timeout"
            return result
        except Exception:
            if order_no and not result.cancel_accepted:
                try:
                    self.api.cancel_order(order_no)
                    result.cancel_accepted = True
                except Exception:
                    pass
            raise


def write_probe_result(path: Path, result: OrderProbeResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
