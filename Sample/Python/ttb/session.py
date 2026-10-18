from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .api import _decoded, response_code, is_success_response


# The TTB local API answers 9999 for account queries while the GUI is not
# logged in to a competition (see the vendor manual: "請先在 TTB 完成登入作業").
NOT_LOGGED_IN_CODES = {"9999"}
DOCUMENTED_MARGIN_FIELD = "AVAILABELMARGIN"


@dataclass
class SessionProbe:
    """Read-only evidence that TTB is logged in and a competition is selected.

    Only response codes and field *names* are kept; balances, account IDs and
    error text never leave this object.
    """

    api_reachable: bool = False
    codes: dict[str, str] = field(default_factory=dict)
    margin_fields: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def logged_in(self) -> bool:
        return self.api_reachable and len(self.codes) == 3 and all(
            code in {"000", "0000"} for code in self.codes.values()
        )

    @property
    def documented_margin_field_present(self) -> bool:
        return DOCUMENTED_MARGIN_FIELD.lower() in {name.lower() for name in self.margin_fields}

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["logged_in"] = self.logged_in
        result["documented_margin_field_present"] = self.documented_margin_field_present
        return result


def _field_names(response: Any) -> list[str]:
    if not is_success_response(response) or not isinstance(response, dict):
        return []
    body = _decoded(response.get("Data", response.get("data")))
    rows = body if isinstance(body, list) else [body]
    names: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            names.update(str(key)[:64] for key in row)
    return sorted(names)


def probe_session(api: Any) -> SessionProbe:
    """Query margin, orders and positions once; never submits or cancels."""
    probe = SessionProbe()
    try:
        margin = api.query_margin()
        orders = api.query_orders()
        positions = api.query_positions()
    except Exception as exc:  # TTB closed, port not listening, malformed reply
        probe.error = type(exc).__name__
        return probe
    probe.api_reachable = True
    probe.codes = {
        "margin": response_code(margin)[:8],
        "orders": response_code(orders)[:8],
        "positions": response_code(positions)[:8],
    }
    probe.margin_fields = _field_names(margin)
    return probe
