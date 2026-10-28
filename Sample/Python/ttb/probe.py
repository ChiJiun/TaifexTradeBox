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
    _value,
    ReportChannelLost,
    find_sent_order,
    is_terminal_order,
    order_number,
)
from .reconciliation import inspect_account


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
    position_query_succeeded: bool = False
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
        # This is deliberately account-wide: a manual position in any symbol
        # must block the only API order probe. The query cannot verify the
        # selected GUI account identity; operator confirmation remains required.
        for _ in range(2):
            inspection = inspect_account(self.api, self.symbol)
            if inspection.open_positions or inspection.working_orders:
                raise RuntimeError("TTB account not flat or has working orders; probe refused")
            time.sleep(self.poll_seconds)
        known = {order_number(row) for row in _response_rows(self.api.query_orders())}
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
            order_no = extract_order_no(response) or self._await_report_row(known, price)
            result.order_no = order_no
            self.api.cancel_order(order_no)
            result.cancel_accepted = True
            deadline = time.monotonic() + self.cancel_timeout
            while time.monotonic() < deadline:
                report = self.api.query_orders()
                rows = [row for row in _response_rows(report) if order_number(row) == order_no]
                reported_fill_quantity = max(
                    (
                        int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0)))
                        for row in rows
                    ),
                    default=0,
                )
                result.unexpected_fill_quantity = max(
                    result.unexpected_fill_quantity,
                    reported_fill_quantity,
                )
                if rows and all(is_terminal_order(row) for row in rows):
                    result.cancel_confirmed = True
                    break
                time.sleep(self.poll_seconds)
            # QUERYRESTOREFILLREPORT is open *position* inventory, not a
            # historical execution list. Never equate missing order IDs with
            # no fills: look for ANY account-wide open position.
            for _ in range(2):
                final_account = inspect_account(self.api, self.symbol)
                result.position_query_succeeded = True
                result.unexpected_fill_quantity = max(
                    result.unexpected_fill_quantity,
                    sum(item["quantity"] for item in final_account.open_positions),
                )
                if final_account.working_orders:
                    result.cancel_confirmed = False
                time.sleep(self.poll_seconds)
            if result.unexpected_fill_quantity:
                result.message = "UNEXPECTED FILL: manually reconcile and flatten the virtual account"
            elif not result.position_query_succeeded:
                result.message = (
                    "post-cancel account position verification failed; "
                    "reconcile the virtual account manually"
                )
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


    def _await_report_row(self, known: set[str], price: float) -> str:
        deadline = time.monotonic() + self.ack_timeout
        while time.monotonic() < deadline:
            rows = _response_rows(self.api.query_orders())
            order_no = find_sent_order(rows, known, self.symbol, self.side, price, 1)
            if order_no:
                return order_no
            time.sleep(self.poll_seconds)
        raise ReportChannelLost("probe order never appeared in the TTB order report; report channel presumed lost")


def write_probe_result(path: Path, result: OrderProbeResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
