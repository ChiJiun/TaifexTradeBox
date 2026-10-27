from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from strategy_core import OrderIntent, StrategyConfig
from .api import _decoded, is_success_response


class PreTradeRiskError(RuntimeError):
    """A pre-trade safety limit blocks new exposure."""


@dataclass(frozen=True)
class PreTradeLimits:
    max_orders_per_minute: int = 20
    max_cancels_per_minute: int = 60
    max_daily_orders: int = 250
    max_notional: float = 0.0
    minimum_available_margin: float = 0.0
    available_margin_field: str = ""
    # TTB QUERYMARGIN returns one row per currency (TWD/USD/JPY/CNY, observed
    # 2026-10-18); the margin field is read from this currency's row only.
    available_margin_currency: str = "TWD"

    def validate(self) -> None:
        if min(self.max_orders_per_minute, self.max_cancels_per_minute, self.max_daily_orders) <= 0:
            raise ValueError("order/cancel limits must be positive")
        if not math.isfinite(self.max_notional) or self.max_notional < 0:
            raise ValueError("max_notional must be a nonnegative finite number")
        if not math.isfinite(self.minimum_available_margin) or self.minimum_available_margin < 0:
            raise ValueError("minimum_available_margin must be a nonnegative finite number")
        if bool(self.available_margin_field) != bool(self.minimum_available_margin):
            raise ValueError("both a verified margin field and a positive minimum margin are required")


def parse_available_margin(response: Any, field: str, currency: str = "TWD") -> float:
    """Parse an explicitly configured margin key; never guess broker semantics.

    The configured key must occur *exactly once* in the rows for ``currency``
    (rows without a CURRENCY field are kept, for single-row responses).
    """
    if not is_success_response(response) or not isinstance(response, dict):
        raise PreTradeRiskError("TTB margin query failed")
    key = field.strip().lower()
    if not key:
        raise PreTradeRiskError("available-margin field is unconfigured")

    content = _decoded(response.get("Data", response.get("data")))
    rows = content if isinstance(content, list) else [content]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise PreTradeRiskError("TTB margin response shape is unverified")
    wanted = currency.strip().upper()
    rows = [
        row for row in rows
        if str(_row_value(row, "currency", "")).strip().upper() in ("", wanted)
    ]
    if not rows:
        raise PreTradeRiskError(f"TTB margin response has no {wanted} row")
    matches = [
        value for row in rows
        for name, value in row.items()
        if str(name).strip().lower() == key
    ]
    if len(matches) != 1:
        raise PreTradeRiskError("TTB margin field missing or ambiguous")
    try:
        number = float(str(matches[0]).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise PreTradeRiskError("TTB available margin is not numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise PreTradeRiskError("TTB available margin is invalid")
    return number


def _row_value(row: dict[str, Any], key: str, default: Any) -> Any:
    for name, value in row.items():
        if str(name).strip().lower() == key:
            return value
    return default


class DurableOrderRateGuard:
    """Append-resistant rate budget shared across normal bot restarts.

    Counts API attempts rather than successes. Persist each attempt *before*
    the network side effect to prevent crash-and-restart budget resets.
    Safety cancellations are never blocked; their count can stop new entries.
    """

    VERSION = 2

    def __init__(self, path: Path, symbol: str, limits: PreTradeLimits) -> None:
        limits.validate()
        self.path = path
        self.symbol = symbol.upper()
        self.limits = limits
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[dict[str, Any]] = []
        self._daily_orders = 0
        self._daily_date = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("version") not in (1, self.VERSION)
                or data.get("symbol") != self.symbol
                or not isinstance(data.get("events"), list)
            ):
                raise ValueError("unsupported risk-counter state")
            events = data["events"]
            if any(
                not isinstance(event, dict)
                or event.get("kind") not in ("order", "cancel")
                or not isinstance(event.get("timestamp"), (int, float))
                or not math.isfinite(event["timestamp"])
                for event in events
            ):
                raise ValueError("invalid rate-counter event")
            if data["version"] == 1:
                # V1 contains every attempt from the preceding two days. Its
                # latest event provides the day for the lossless active-day
                # migration; never silently zero a valid historical count.
                latest = max((event["timestamp"] for event in events), default=0)
                day = self._taipei_date(latest) if events else ""
                self._daily_date = day
                self._daily_orders = sum(
                    1 for event in events
                    if event["kind"] == "order"
                    and self._taipei_date(event["timestamp"]) == day
                )
                self._events = [
                    event for event in events
                    if latest - 60 < event["timestamp"] <= latest
                ] if events else []
            else:
                if (
                    not isinstance(data.get("daily_orders"), int)
                    or isinstance(data.get("daily_orders"), bool)
                    or data["daily_orders"] < 0
                    or not isinstance(data.get("daily_date"), str)
                ):
                    raise ValueError("invalid daily counters")
                self._events = events
                self._daily_orders = data["daily_orders"]
                self._daily_date = data["daily_date"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise PreTradeRiskError(f"risk counters could not be restored; fail closed: {exc}") from exc

    @staticmethod
    def _taipei_date(now: float) -> str:
        return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()

    def _counts(self, now: float) -> tuple[int, int, int]:
        if any(event["timestamp"] > now + 1 for event in self._events):
            raise PreTradeRiskError("system clock moved backward relative to saved order attempts")
        today = self._taipei_date(now)
        if self._daily_date and today < self._daily_date:
            raise PreTradeRiskError("system clock moved backward relative to saved daily counter")
        minute_orders = sum(1 for event in self._events
                            if event["kind"] == "order" and 0 <= now - event["timestamp"] < 60)
        minute_cancels = sum(1 for event in self._events
                             if event["kind"] == "cancel" and 0 <= now - event["timestamp"] < 60)
        daily_orders = self._daily_orders if today == self._daily_date else 0
        return minute_orders, minute_cancels, daily_orders

    def assert_entry_budget(self, now: float) -> None:
        with self._lock:
            order_rate, cancel_rate, order_day = self._counts(now)
            if order_rate >= self.limits.max_orders_per_minute:
                raise PreTradeRiskError("max orders per minute reached")
            if cancel_rate >= self.limits.max_cancels_per_minute:
                raise PreTradeRiskError("max cancels per minute reached; further entries blocked")
            if order_day >= self.limits.max_daily_orders:
                raise PreTradeRiskError("max daily order attempts reached")

    def record(self, kind: str, now: float) -> None:
        if kind not in {"order", "cancel"}:
            raise ValueError("invalid risk-counter event kind")
        if not math.isfinite(now):
            raise PreTradeRiskError("invalid current timestamp")
        with self._lock:
            self._counts(now)  # also detects backward clock changes
            today = self._taipei_date(now)
            events = [
                event for event in self._events
                if 0 <= now - event["timestamp"] < 60
            ]
            events.append({"kind": kind, "timestamp": now})
            daily_orders = (
                self._daily_orders if self._daily_date == today else 0
            ) + int(kind == "order")
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump({
                        "version": self.VERSION,
                        "symbol": self.symbol,
                        "daily_date": today,
                        "daily_orders": daily_orders,
                        "events": events,
                    }, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self._events = events
            self._daily_orders = daily_orders
            self._daily_date = today

    def snapshot(self, now: float) -> dict[str, int]:
        with self._lock:
            orders, cancels, daily = self._counts(now)
            return {"orders_last_minute": orders, "cancels_last_minute": cancels, "orders_today_taipei": daily}


def validate_exposure(
    intent: OrderIntent,
    position: int,
    active: dict[str, dict[str, Any]],
    config: StrategyConfig,
    limits: PreTradeLimits,
) -> None:
    if intent.quantity <= 0 or int(intent.quantity) != intent.quantity:
        raise PreTradeRiskError("order quantity must be a positive integer")
    if not math.isfinite(intent.price) or intent.price <= 0:
        raise PreTradeRiskError("order price is invalid")
    if intent.role in ("exit", "risk_exit"):
        if not position or (
            (position > 0 and intent.side != "sell")
            or (position < 0 and intent.side != "buy")
        ) or intent.quantity > abs(position):
            raise PreTradeRiskError("exit is not proven reduce-only")
        return
    if intent.role != "entry":
        raise PreTradeRiskError("unknown order role")
    buys = intent.quantity if intent.side == "buy" else 0
    sells = intent.quantity if intent.side == "sell" else 0
    for side, current in active.items():
        amount = int(current.get("qty", 0))
        if amount < 0:
            raise PreTradeRiskError("working quantity is invalid")
        buys += amount if side == "buy" else 0
        sells += amount if side == "sell" else 0
    possible_long = abs(position + buys)
    possible_short = abs(position - sells)
    exposure = max(possible_long, possible_short)
    if exposure > config.max_inventory:
        raise PreTradeRiskError("entry exceeds max_inventory including working orders")
    if limits.max_notional <= 0:
        raise PreTradeRiskError("max-notional must be configured before TTB live entries")
    max_price = max(
        [intent.price] + [float(order.get("price", intent.price)) for order in active.values()]
    )
    if not math.isfinite(max_price) or max_price <= 0:
        raise PreTradeRiskError("working order price is invalid")
    if exposure * max_price * config.point_value > limits.max_notional:
        raise PreTradeRiskError("entry exceeds configured max notional")


def assess_pretrade(
    intent: OrderIntent,
    position: int,
    active: dict[str, dict[str, Any]],
    config: StrategyConfig,
    limits: PreTradeLimits,
    *,
    order_rate: DurableOrderRateGuard,
    now: float,
    margin_supplier: Callable[[], Any],
) -> None:
    """Identical live/offline pre-trade risk policy, with injected margin data.

    The caller is responsible for persisting the order attempt AFTER success
    and BEFORE the network side effect. `margin_supplier` may be a fake
    provider in offline research; unknown TTB fields still fail closed.
    """
    if intent.quantity > config.max_order_qty:
        raise PreTradeRiskError("order exceeds configured max_order_qty")
    validate_exposure(intent, position, active, config, limits)
    if intent.role != "entry":
        # Validated reduce-only exits are intentionally not subject to the
        # entry margin threshold or order-budget exhaustion.
        return
    order_rate.assert_entry_budget(now)
    if not limits.available_margin_field or limits.minimum_available_margin <= 0:
        raise PreTradeRiskError(
            "live entries require a verified --available-margin-field and --min-available-margin"
        )
    margin = parse_available_margin(
        margin_supplier(), limits.available_margin_field, limits.available_margin_currency,
    )
    if margin < limits.minimum_available_margin:
        raise PreTradeRiskError("available TTB margin below required headroom")
