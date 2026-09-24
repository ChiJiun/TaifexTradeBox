from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal


Side = Literal["buy", "sell"]
SUCCESS_CODES = {"000", "0000"}


class TTBApiError(RuntimeError):
    """Raised when TTB rejects an API command."""


def response_code(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    return str(response.get("Code", response.get("code", "")))


def is_success_response(response: Any) -> bool:
    return response_code(response) in SUCCESS_CODES


def require_success(action: str, response: Any) -> Any:
    if not is_success_response(response):
        raise TTBApiError(f"TTB {action} failed: {response}")
    return response


def _decoded(value: Any) -> Any:
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


def extract_order_no(response: Any) -> str:
    """Return an order number from the response shapes used by TTB.

    Some TTB versions return the number at the top level, while others place
    it in a JSON-encoded ``Data`` field.  Returning an empty string keeps the
    caller fail-closed so it can reconcile the order report before cancelling.
    """

    current = _decoded(response)
    if not isinstance(current, dict):
        return ""
    lowered = {str(key).lower(): value for key, value in current.items()}
    for key in ("ordno", "orderno", "order_no"):
        value = lowered.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("data", "result"):
        nested = _decoded(lowered.get(key))
        if isinstance(nested, list):
            for row in nested:
                order_no = extract_order_no(row)
                if order_no:
                    return order_no
        else:
            order_no = extract_order_no(nested)
            if order_no:
                return order_no
    return ""


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else format(value, "f").rstrip("0").rstrip(".")


@dataclass(frozen=True)
class TTBOrderRequest:
    """Parameters for one TTB limit order.

    The defaults match the fields used by the official Python sample.  They
    still apply to the currently logged-in TTB virtual account, so callers
    must select the correct competition/account in TTB first.
    """

    symbol: str
    side: Side
    price: float
    quantity: int = 1
    time_in_force: str = "1"
    order_type: str = "2"
    day_trade: bool = False
    position_effect: str = "4"
    symbol2: str = ""
    side2: str = ""

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if self.price <= 0:
            raise ValueError("price must be positive")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    def payload(self) -> dict[str, str]:
        return {
            "Symbol1": self.symbol.upper(),
            "Price": _number_text(self.price),
            "TimeInForce": self.time_in_force,
            "Side1": "1" if self.side == "buy" else "2",
            "OrderType": self.order_type,
            "OrderQty": str(self.quantity),
            "DayTrade": "1" if self.day_trade else "0",
            "Symbol2": self.symbol2,
            "Side2": self.side2,
            "PositionEffect": self.position_effect,
        }


class TTBApi:
    """Small typed facade over the methods supplied by ``TTBHelp.pyc``."""

    def __init__(self, module: Any) -> None:
        self.module = module

    @classmethod
    def connect(cls, host: str = "http://localhost:8080", zmq_port: int = 51141) -> "TTBApi":
        # Import lazily so shadow/unit-test code can run without loading the
        # bundled Windows-only helper.
        from TTBHelp import TTBModule

        class TTBProcess(TTBModule):
            def SHOWQUOTEDATA(self, obj: dict[str, Any]) -> None:
                # Quotes are consumed by the stoppable ZMQ subscriber.
                pass

        return cls(TTBProcess(host, zmq_port))

    def subscribe_quote(self, symbol: str) -> Any:
        if not symbol.strip():
            raise ValueError("symbol is required")
        return self.module.QUOTEDATA(symbol.upper())

    def new_order(self, request: TTBOrderRequest) -> Any:
        return require_success("new order", self.module.NEWORDER(request.payload()))

    def cancel_order(self, order_no: str) -> Any:
        if not order_no.strip():
            raise ValueError("order_no is required")
        return require_success("cancel order", self.module.CANCELORDER({"OrdNo": order_no}))

    def replace_price(self, order_no: str, price: float) -> Any:
        if not order_no.strip() or price <= 0:
            raise ValueError("order_no and a positive price are required")
        payload = {"OrdNo": order_no, "Price": _number_text(price)}
        return require_success("replace price", self.module.REPLACEPRICE(payload))

    def replace_quantity(self, order_no: str, quantity: int) -> Any:
        if not order_no.strip() or quantity <= 0:
            raise ValueError("order_no and a positive quantity are required")
        payload = {"OrdNo": order_no, "UdpQty": str(quantity)}
        return require_success("replace quantity", self.module.REPLACEQTY(payload))

    def query_margin(self) -> Any:
        return self.module.QUERYMARGIN()

    def query_fills(self) -> Any:
        return self.module.QUERYRESTOREFILLREPORT()

    def query_orders(self) -> Any:
        return self.module.QUERYRESTOREREPORT()
