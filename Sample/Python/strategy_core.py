from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Deque, Iterable, Literal


Side = Literal["buy", "sell"]


def _number(value: Any, default: float = 0.0) -> float:
    """Convert TTB strings such as '40,123.00' to a float."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return _number(value[0], default) if value else default
    if isinstance(value, dict):
        for key in ("Price", "price", "Value", "value"):
            if key in value:
                return _number(value[key], default)
        return default
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "None"}:
        return default
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        return default


def _field(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        if name in data:
            return data[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    last: float
    last_qty: int
    total_volume: float
    exchange_time: str
    received_at: float

    @classmethod
    def from_ttb(cls, data: dict[str, Any]) -> "Quote":
        return cls(
            symbol=str(_field(data, "Symbol", "symbol", default="")).upper(),
            bid=_number(_field(data, "BidPs", "BidPrice", "bid")),
            ask=_number(_field(data, "AskPs", "AskPrice", "ask")),
            bid_qty=_number(_field(data, "BidPv", "BidQty", "bid_qty")),
            ask_qty=_number(_field(data, "AskPv", "AskQty", "ask_qty")),
            last=_number(_field(data, "Price", "Last", "last")),
            last_qty=max(0, int(_number(_field(data, "Qty", "LTQ", "last_qty")))),
            total_volume=_number(_field(data, "Volume", "TotalVolume", "total_volume")),
            exchange_time=str(_field(data, "TickTime", "Time", "time", default="")),
            received_at=time.monotonic(),
        )

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > self.bid and self.last > 0


@dataclass
class StrategyConfig:
    symbol: str
    tick_size: float = 1.0
    point_value: float = 200.0
    fee_per_fill: float = 0.0
    tax_rate: float = 0.0
    slippage_ticks: float = 0.0
    target_ticks: int = 3
    stop_ticks: int = 8
    max_hold_seconds: float = 15.0
    signal_threshold: float = 0.16
    toxic_threshold: float = 0.55
    flow_window_seconds: float = 2.0
    base_qty: int = 1
    max_order_qty: int = 4
    max_inventory: int = 8
    recovery_fraction: float = 0.25
    daily_loss_limit: float = 50_000.0
    min_quote_interval_ms: int = 200

    def validate(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.tick_size <= 0 or self.point_value <= 0:
            raise ValueError("tick_size and point_value must be positive")
        if self.target_ticks <= 0 or self.stop_ticks <= 0:
            raise ValueError("target_ticks and stop_ticks must be positive")
        if not 0 <= self.recovery_fraction <= 1:
            raise ValueError("recovery_fraction must be between 0 and 1")
        if self.base_qty <= 0 or self.max_order_qty < self.base_qty:
            raise ValueError("invalid order quantity limits")
        if self.max_inventory < self.max_order_qty:
            raise ValueError("max_inventory must be >= max_order_qty")

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value

    def estimated_round_trip_cost(self, price: float) -> float:
        tax = abs(price * self.point_value * self.tax_rate)
        slippage = self.slippage_ticks * self.tick_value
        return 2.0 * (self.fee_per_fill + tax) + slippage

    def estimated_cycle_net(self, price: float) -> float:
        return self.target_ticks * self.tick_value - self.estimated_round_trip_cost(price)

    def minimum_profitable_ticks(self, price: float) -> int:
        cost = self.estimated_round_trip_cost(price)
        return max(1, math.floor(cost / self.tick_value) + 1)


@dataclass(frozen=True)
class Features:
    imbalance: float
    microprice: float
    micro_bias_ticks: float
    trade_flow: float
    score: float


class FeatureEngine:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self._trades: Deque[tuple[float, int, int]] = deque()
        self._previous_last = 0.0
        self._last_direction = 0

    def update(self, quote: Quote) -> Features:
        depth = quote.bid_qty + quote.ask_qty
        imbalance = (quote.bid_qty - quote.ask_qty) / depth if depth > 0 else 0.0
        microprice = (
            (quote.ask * quote.bid_qty + quote.bid * quote.ask_qty) / depth
            if depth > 0
            else quote.mid
        )
        micro_bias_ticks = (microprice - quote.mid) / self.config.tick_size

        direction = self._classify_trade(quote)
        if quote.last_qty > 0 and direction:
            self._trades.append((quote.received_at, direction, quote.last_qty))
        cutoff = quote.received_at - self.config.flow_window_seconds
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()
        total = sum(qty for _, _, qty in self._trades)
        trade_flow = (
            sum(direction * qty for _, direction, qty in self._trades) / total
            if total
            else 0.0
        )
        score = 0.50 * imbalance + 0.25 * max(-1.0, min(1.0, micro_bias_ticks)) + 0.25 * trade_flow
        return Features(imbalance, microprice, micro_bias_ticks, trade_flow, score)

    def _classify_trade(self, quote: Quote) -> int:
        if quote.last >= quote.ask:
            direction = 1
        elif quote.last <= quote.bid:
            direction = -1
        elif self._previous_last and quote.last > self._previous_last:
            direction = 1
        elif self._previous_last and quote.last < self._previous_last:
            direction = -1
        else:
            direction = self._last_direction
        self._previous_last = quote.last
        if direction:
            self._last_direction = direction
        return direction


@dataclass(frozen=True)
class Fill:
    side: Side
    price: float
    quantity: int
    order_id: str = ""
    occurred_at: float = 0.0


class PositionLedger:
    """FIFO-equivalent average-price ledger for one futures contract."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.position = 0
        self.average_price = 0.0
        self.realized_pnl = 0.0
        self.peak_realized_pnl = 0.0
        self.fees_and_tax = 0.0
        self.opened_at = 0.0
        self.fill_count = 0

    @property
    def recovery_debt(self) -> float:
        return max(0.0, self.peak_realized_pnl - self.realized_pnl)

    def on_fill(self, fill: Fill) -> float:
        if fill.quantity <= 0:
            return 0.0
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        old_position = self.position
        realized_delta = 0.0
        cost = fill.quantity * (
            self.config.fee_per_fill
            + abs(fill.price * self.config.point_value * self.config.tax_rate)
        )

        if old_position == 0 or old_position * signed > 0:
            new_position = old_position + signed
            self.average_price = (
                abs(old_position) * self.average_price + abs(signed) * fill.price
            ) / abs(new_position)
            self.position = new_position
            if old_position == 0:
                self.opened_at = fill.occurred_at or time.monotonic()
        else:
            closing_qty = min(abs(old_position), abs(signed))
            if old_position > 0:
                realized_delta = (fill.price - self.average_price) * closing_qty * self.config.point_value
            else:
                realized_delta = (self.average_price - fill.price) * closing_qty * self.config.point_value
            new_position = old_position + signed
            self.position = new_position
            if new_position == 0:
                self.average_price = 0.0
                self.opened_at = 0.0
            elif old_position * new_position < 0:
                self.average_price = fill.price
                self.opened_at = fill.occurred_at or time.monotonic()

        self.fill_count += fill.quantity
        self.fees_and_tax += cost
        realized_delta -= cost
        self.realized_pnl += realized_delta
        self.peak_realized_pnl = max(self.peak_realized_pnl, self.realized_pnl)
        return realized_delta

    def mark_to_market(self, price: float) -> float:
        if self.position > 0:
            unrealized = (price - self.average_price) * self.position * self.config.point_value
        elif self.position < 0:
            unrealized = (self.average_price - price) * abs(self.position) * self.config.point_value
        else:
            unrealized = 0.0
        return self.realized_pnl + unrealized


class SoftRecoverySizer:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def quantity(self, debt: float, price: float, inventory: int) -> int:
        cycle_net = self.config.estimated_cycle_net(price)
        if cycle_net <= 0:
            return 0
        recovery = math.ceil(self.config.recovery_fraction * debt / cycle_net) if debt else 0
        desired = min(self.config.max_order_qty, self.config.base_qty + recovery)
        capacity = max(0, self.config.max_inventory - abs(inventory))
        return min(desired, capacity)


@dataclass(frozen=True)
class OrderIntent:
    side: Side
    price: float
    quantity: int
    role: Literal["entry", "exit", "risk_exit"]
    reason: str


@dataclass(frozen=True)
class StrategyDecision:
    intents: tuple[OrderIntent, ...]
    features: Features
    halted: bool
    reason: str


class IndependentFillStrategy:
    """Simulator-specific passive strategy for independent price/volume matching."""

    def __init__(self, config: StrategyConfig, ledger: PositionLedger | None = None) -> None:
        config.validate()
        self.config = config
        self.ledger = ledger or PositionLedger(config)
        self.features = FeatureEngine(config)
        self.sizer = SoftRecoverySizer(config)

    def decide(self, quote: Quote) -> StrategyDecision:
        features = self.features.update(quote)
        if not quote.is_valid():
            return StrategyDecision((), features, True, "invalid quote")
        if self.ledger.realized_pnl <= -abs(self.config.daily_loss_limit):
            return StrategyDecision((), features, True, "daily loss limit")
        if self.config.estimated_cycle_net(quote.last) <= 0:
            return StrategyDecision((), features, True, "target does not cover configured costs")

        position = self.ledger.position
        now = quote.received_at
        if position > 0:
            stop_price = self.ledger.average_price - self.config.stop_ticks * self.config.tick_size
            timed_out = self.ledger.opened_at and now - self.ledger.opened_at >= self.config.max_hold_seconds
            if quote.last <= stop_price or timed_out:
                return StrategyDecision(
                    (OrderIntent("sell", quote.bid, abs(position), "risk_exit", "stop or timeout"),),
                    features,
                    False,
                    "long risk exit",
                )
            target = self.ledger.average_price + self.config.target_ticks * self.config.tick_size
            price = max(target, quote.ask)
            return StrategyDecision(
                (OrderIntent("sell", price, abs(position), "exit", "long profit target"),),
                features,
                False,
                "manage long",
            )

        if position < 0:
            stop_price = self.ledger.average_price + self.config.stop_ticks * self.config.tick_size
            timed_out = self.ledger.opened_at and now - self.ledger.opened_at >= self.config.max_hold_seconds
            if quote.last >= stop_price or timed_out:
                return StrategyDecision(
                    (OrderIntent("buy", quote.ask, abs(position), "risk_exit", "stop or timeout"),),
                    features,
                    False,
                    "short risk exit",
                )
            target = self.ledger.average_price - self.config.target_ticks * self.config.tick_size
            price = min(target, quote.bid)
            return StrategyDecision(
                (OrderIntent("buy", price, abs(position), "exit", "short profit target"),),
                features,
                False,
                "manage short",
            )

        quantity = self.sizer.quantity(self.ledger.recovery_debt, quote.last, 0)
        if quantity <= 0:
            return StrategyDecision((), features, True, "no profitable size")
        if features.score >= self.config.signal_threshold and features.trade_flow > -self.config.toxic_threshold:
            intent = OrderIntent("buy", quote.bid, quantity, "entry", "positive book/flow score")
            return StrategyDecision((intent,), features, False, "long entry")
        if features.score <= -self.config.signal_threshold and features.trade_flow < self.config.toxic_threshold:
            intent = OrderIntent("sell", quote.ask, quantity, "entry", "negative book/flow score")
            return StrategyDecision((intent,), features, False, "short entry")
        return StrategyDecision((), features, False, "signal neutral")


@dataclass
class SimulatedOrder:
    order_id: str
    intent: OrderIntent
    remaining: int
    created_at: float


class IndependentFillSimulator:
    """Fill resting orders when a later real tick reaches their price and volume."""

    def __init__(self) -> None:
        self.orders: dict[Side, SimulatedOrder] = {}
        self._sequence = 0

    def sync(self, intents: Iterable[OrderIntent], now: float) -> None:
        desired = {intent.side: intent for intent in intents if intent.quantity > 0}
        for side in list(self.orders):
            current = self.orders[side]
            wanted = desired.get(side)
            if wanted is None or wanted.price != current.intent.price or wanted.quantity != current.remaining:
                del self.orders[side]
        for side, intent in desired.items():
            if side not in self.orders:
                self._sequence += 1
                self.orders[side] = SimulatedOrder(
                    f"SIM{self._sequence:08d}", intent, intent.quantity, now
                )

    def on_quote(self, quote: Quote) -> list[Fill]:
        fills: list[Fill] = []
        available = max(0, quote.last_qty)
        if available <= 0:
            return fills
        for side in ("buy", "sell"):
            order = self.orders.get(side)
            if order is None or quote.received_at <= order.created_at:
                continue
            reached = quote.last <= order.intent.price if side == "buy" else quote.last >= order.intent.price
            if not reached:
                continue
            quantity = min(order.remaining, available)
            if quantity <= 0:
                continue
            fills.append(Fill(side, quote.last, quantity, order.order_id, quote.received_at))
            order.remaining -= quantity
            available -= quantity
            if order.remaining <= 0:
                del self.orders[side]
            if available <= 0:
                break
        return fills
