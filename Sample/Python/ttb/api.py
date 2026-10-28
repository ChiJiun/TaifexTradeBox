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


def position_effect_for(side: Side, quantity: int, position: int) -> str:
    """TTB's PositionEffect: "Y" closes, "N" opens.

    TTB 2026.07.08 rejects anything else (the manual's 0/1/4 included) with
    平倉格式錯誤 and refuses "Y" beyond the held quantity, so only an order that
    fully fits inside the opposite position is sent as a close.
    """
    reduces = (position > 0 and side == "sell") or (position < 0 and side == "buy")
    return "Y" if reduces and quantity <= abs(position) else "N"


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
    position_effect: str = "N"
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
        if self.position_effect not in ("Y", "N"):
            raise ValueError("position_effect must be 'Y' (close) or 'N' (open)")

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


def ttbhelp_version() -> str:
    """Version string the bundled TTBHelp (.py or .pyc) reports to TTB."""
    from TTBHelp import TTBHelp

    return str(TTBHelp._TTBHelp__ShowVersion(None))  # type: ignore[attr-defined]


class HttpTTBModule:
    """The TTBHelp endpoints over HTTP, with timeouts and real exceptions.

    TTBHelp's constructor calls ``os._exit(0)`` when TTB is unreachable or
    rejects the version handshake, which a supervisor cannot tell apart from a
    clean exit, and its requests have no timeout. Method names and payloads
    are identical to TTBHelp so ``TTBApi`` and existing tests are unchanged.
    """

    def __init__(self, host: str, timeout: float = 10.0, session: Any = None) -> None:
        import requests

        self.host = host.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _post(self, endpoint: str, payload: Any = None) -> Any:
        response = self.session.post(f"{self.host}/api/Trade/{endpoint}", json=payload, timeout=self.timeout)
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise TTBApiError(f"TTB {endpoint} returned non-JSON (HTTP {response.status_code})") from exc

    def handshake(self, version: str) -> None:
        reply = self._post("CONNECT", {"Ver": version})
        message = str(reply.get("ErrMsg", "")) if isinstance(reply, dict) else "invalid CONNECT reply"
        if message:
            raise TTBApiError(f"TTB rejected API handshake for TTBHelp {version}: {message}")

    def QUOTEDATA(self, symbols: str) -> Any:
        # TTBHelp only opens a local ZMQ SUB here; TTB publishes the symbols
        # added in its GUI quote settings. QuoteMonitor owns the subscriber.
        return {"Code": "000", "Message": ""}

    def NEWORDER(self, payload: dict[str, str]) -> Any:
        return self._post("NEWORDER", payload)

    def CANCELORDER(self, payload: dict[str, str]) -> Any:
        return self._post("CANCELORDER", payload)

    def REPLACEPRICE(self, payload: dict[str, str]) -> Any:
        return self._post("REPLACEPRICE", payload)

    def REPLACEQTY(self, payload: dict[str, str]) -> Any:
        return self._post("REPLACEQTY", payload)

    def QUERYRESTOREREPORT(self) -> Any:
        return self._post("QUERYRESTOREREPORT")

    def QUERYMARGIN(self) -> Any:
        return self._post("QUERYMARGIN")

    def QUERYRESTOREFILLREPORT(self) -> Any:
        return self._post("QUERYRESTOREFILLREPORT")


class TTBApi:
    """Small typed facade over the TTB local API (TTBHelp method names)."""

    def __init__(self, module: Any) -> None:
        self.module = module

    @classmethod
    def connect(cls, host: str = "http://localhost:8080", zmq_port: int = 51141) -> "TTBApi":
        # zmq_port is kept for call-site compatibility; quotes are consumed by
        # ttb.market_data's own subscriber. Version comes from TTBHelp so the
        # handshake matches the TTB.exe it was shipped with.
        module = HttpTTBModule(host)
        module.handshake(ttbhelp_version())
        return cls(module)

    def subscribe_quote(self, symbol: str) -> Any:
        if not symbol.strip():
            raise ValueError("symbol is required")
        return self.module.QUOTEDATA(symbol.upper())

    def new_order(self, request: TTBOrderRequest) -> Any:
        return require_success("new order", self.module.NEWORDER(request.payload()))

    def new_market_ioc(self, symbol: str, side: Side, quantity: int) -> Any:
        """Market (OrderType=1) IOC close order; only used by automated flattening.

        TTB ignores Price for market orders per the vendor manual, so "0" is
        sent. Callers must verify the resulting position from the broker
        report; acceptance alone proves nothing about the fill.
        """
        payload = TTBOrderRequest(
            symbol=symbol, side=side, price=1, quantity=quantity,
            time_in_force="2", order_type="1", position_effect="Y",
        ).payload()
        payload["Price"] = "0"
        return require_success("market IOC order", self.module.NEWORDER(payload))

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

    def query_positions(self) -> Any:
        """Return the current open-position report.

        The official TTB manual names the endpoint RESTOREFILLREPORT, but
        documents it as the inventory/open-position query. Keep the clearer
        facade name here so recovery code does not mistake it for execution
        history.
        """
        return self.module.QUERYRESTOREFILLREPORT()

    def query_fills(self) -> Any:
        """Backward-compatible alias; new code should use query_positions()."""
        return self.query_positions()

    def query_orders(self) -> Any:
        return self.module.QUERYRESTOREREPORT()
