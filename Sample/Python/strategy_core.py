from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Deque, Iterable, Literal


Side = Literal["buy", "sell"]
FillModel = Literal["independent", "probabilistic", "queue"]
SizingMode = Literal["fixed", "martingale", "anti_martingale"]


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
        values = (self.bid, self.ask, self.last, self.bid_qty, self.ask_qty, self.total_volume, self.received_at)
        return all(math.isfinite(x) for x in values) and self.bid > 0 and self.ask > self.bid and self.last > 0 and min(self.bid_qty, self.ask_qty, self.total_volume) >= 0


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
    half_width_ticks: float = 4.0
    warmup_ticks: int = 30
    max_spread_ticks: float = 4.0
    volatility_limit_ticks: float = 8.0
    max_drawdown: float = 40_000.0
    quote_ttl_seconds: float = 5.0
    reprice_threshold_ticks: float = 4.0
    virtual_max_mode: bool = False
    sizing_mode: SizingMode = "fixed"
    sizing_multiplier: float = 2.0
    max_sizing_step: int = 4
    entry_offset_ticks: float = 0.0

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
        if self.half_width_ticks <= 0 or self.warmup_ticks < 2 or self.max_spread_ticks <= 0 or self.volatility_limit_ticks <= 0:
            raise ValueError("invalid market-making settings")
        if self.daily_loss_limit <= 0 or self.max_drawdown <= 0 or self.max_hold_seconds <= 0:
            raise ValueError("risk limits must be positive")
        if self.quote_ttl_seconds <= 0 or self.reprice_threshold_ticks <= 0:
            raise ValueError("quote lifetime and reprice threshold must be positive")
        if self.sizing_mode not in {"fixed", "martingale", "anti_martingale"}:
            raise ValueError("unknown sizing mode")
        if self.sizing_multiplier < 1 or self.max_sizing_step < 0 or self.entry_offset_ticks < 0:
            raise ValueError("invalid virtual-max sizing or entry offset")

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
        self._volume = None

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
        increment = max(0, quote.total_volume - self._volume) if self._volume is not None else 0
        self._volume = quote.total_volume
        if quote.last_qty > 0 and direction and increment > 0:
            self._trades.append((quote.received_at, direction, min(quote.last_qty, int(increment))))
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
    queue_ahead: float = 0.0


class MicropriceMarketMaker(IndependentFillStrategy):
    """Research strategy: fixed-size, inventory-limited, event-driven quoting.

    Scores are heuristic, not calibrated probabilities. Loss limits latch for
    this process lifetime; starting a new session requires an explicit restart.
    """

    def __init__(self, config: StrategyConfig, ledger: PositionLedger | None = None) -> None:
        super().__init__(config, ledger)
        self.history = deque(maxlen=300)
        self.previous = None
        self.halt_reason = ""
        self.peak_equity = 0.0
        self.cycle_target = None
        self.previous_position = 0
        self.entry_intents: tuple[OrderIntent, ...] = ()
        self.entry_quote_created_at = 0.0
        self.entry_quote_center = 0.0
        self.observed_position = self.ledger.position
        self.flat_realized_baseline = self.ledger.realized_pnl
        self.win_streak = 0
        self.loss_streak = 0
        self.last_cycle_net = 0.0

    def _clear_entry_quote(self) -> None:
        self.entry_intents = ()
        self.entry_quote_created_at = 0.0
        self.entry_quote_center = 0.0

    def _observe_cycle(self) -> None:
        position = self.ledger.position
        if self.observed_position != 0 and position == 0:
            self.last_cycle_net = self.ledger.realized_pnl - self.flat_realized_baseline
            if self.last_cycle_net > 0:
                self.win_streak += 1
                self.loss_streak = 0
            elif self.last_cycle_net < 0:
                self.loss_streak += 1
                self.win_streak = 0
            self.flat_realized_baseline = self.ledger.realized_pnl
        self.observed_position = position

    def _entry_quantity(self) -> int:
        cfg = self.config
        step = 0
        if cfg.sizing_mode == "martingale":
            step = self.loss_streak
        elif cfg.sizing_mode == "anti_martingale":
            step = self.win_streak
        step = min(step, cfg.max_sizing_step)
        desired = math.ceil(cfg.base_qty * cfg.sizing_multiplier ** step)
        return min(desired, cfg.max_order_qty, cfg.max_inventory)

    def decide(self, quote: Quote) -> StrategyDecision:
        if not quote.is_valid() or quote.symbol.upper() != self.config.symbol.upper():
            return StrategyDecision((), Features(0, 0, 0, 0, 0), True, "invalid quote")
        cfg = self.config
        self._observe_cycle()
        features = self.features.update(quote)
        ofi = 0.0
        if self.previous is not None:
            p = self.previous
            ofi = ((quote.bid_qty if quote.bid >= p.bid else 0) - (p.bid_qty if quote.bid <= p.bid else 0)
                   - (quote.ask_qty if quote.ask <= p.ask else 0) + (p.ask_qty if quote.ask >= p.ask else 0))
            ofi /= max(1, quote.bid_qty + quote.ask_qty + p.bid_qty + p.ask_qty)
        self.previous = quote
        self.history.append((quote.received_at, quote.mid))
        while self.history and self.history[0][0] < quote.received_at - 30:
            self.history.popleft()
        prices = [p for _, p in self.history]
        mean = sum(prices) / len(prices)
        vol = math.sqrt(sum((p - mean) ** 2 for p in prices) / len(prices)) / cfg.tick_size
        score = .75 * features.score + .25 * ofi
        features = Features(features.imbalance, features.microprice, features.micro_bias_ticks, features.trade_flow, score)
        pos = self.ledger.position
        liquidation = quote.bid if pos > 0 else quote.ask
        equity = self.ledger.mark_to_market(liquidation)
        equity -= abs(pos) * (cfg.fee_per_fill + liquidation * cfg.point_value * cfg.tax_rate)
        self.peak_equity = max(self.peak_equity, equity)
        if equity <= -cfg.daily_loss_limit:
            self.halt_reason = "session loss limit"
        if self.peak_equity - equity >= cfg.max_drawdown:
            self.halt_reason = "maximum drawdown"
        if abs(pos) > cfg.max_inventory:
            self.halt_reason = "inventory limit"
        if pos:
            self._clear_entry_quote()
            side = "sell" if pos > 0 else "buy"
            sign = 1 if pos > 0 else -1
            adverse = sign * (liquidation - self.ledger.average_price) / cfg.tick_size
            timed_out = quote.received_at - self.ledger.opened_at >= cfg.max_hold_seconds
            if self.halt_reason or adverse <= -cfg.stop_ticks or timed_out or sign * score < -cfg.toxic_threshold:
                return StrategyDecision((OrderIntent(side, liquidation, abs(pos), "risk_exit", self.halt_reason or "stop/time/flow"),), features, bool(self.halt_reason), "reduce inventory")
            if not self.previous_position or self.previous_position * pos < 0:
                self.cycle_target = self.ledger.average_price + sign * cfg.target_ticks * cfg.tick_size
            self.previous_position = pos
            target = self.cycle_target
            rounded = (math.ceil(target / cfg.tick_size) if pos > 0 else math.floor(target / cfg.tick_size)) * cfg.tick_size
            return StrategyDecision((OrderIntent(side, rounded, abs(pos), "exit", "fixed cycle target"),), features, False, "manage inventory")
        self.previous_position = 0
        self.cycle_target = None
        if self.halt_reason:
            self._clear_entry_quote()
            return StrategyDecision((), features, True, self.halt_reason)
        if len(prices) < cfg.warmup_ticks:
            self._clear_entry_quote()
            return StrategyDecision((), features, False, "warmup")
        if (quote.ask - quote.bid) / cfg.tick_size > cfg.max_spread_ticks or vol > cfg.volatility_limit_ticks:
            self._clear_entry_quote()
            return StrategyDecision((), features, False, "spread/volatility filter")
        if cfg.estimated_cycle_net(quote.mid) <= 0:
            self._clear_entry_quote()
            return StrategyDecision((), features, False, "cost filter")
        center = features.microprice + score * cfg.tick_size
        if cfg.virtual_max_mode:
            buy = math.floor(
                (quote.bid - cfg.entry_offset_ticks * cfg.tick_size) / cfg.tick_size
            ) * cfg.tick_size
            sell = math.ceil(
                (quote.ask + cfg.entry_offset_ticks * cfg.tick_size) / cfg.tick_size
            ) * cfg.tick_size
        else:
            width = max(cfg.half_width_ticks, cfg.minimum_profitable_ticks(quote.mid) / 2 + 1, vol)
            buy = min(quote.bid, math.floor(center / cfg.tick_size - width) * cfg.tick_size)
            sell = max(quote.ask, math.ceil(center / cfg.tick_size + width) * cfg.tick_size)
        qty = self._entry_quantity()
        intents = []
        if score >= -cfg.signal_threshold:
            intents.append(OrderIntent("buy", buy, qty, "entry", "microprice band"))
        if score <= cfg.signal_threshold:
            intents.append(OrderIntent("sell", sell, qty, "entry", "microprice band"))
        candidate = tuple(intents)
        quote_expired = (
            not self.entry_intents
            or quote.received_at - self.entry_quote_created_at >= cfg.quote_ttl_seconds
            or abs(center - self.entry_quote_center) / cfg.tick_size >= cfg.reprice_threshold_ticks
        )
        if quote_expired:
            self.entry_intents = candidate
            self.entry_quote_created_at = quote.received_at
            self.entry_quote_center = center
        else:
            previous = {intent.side: intent for intent in self.entry_intents}
            # Directional filters may cancel one side immediately.  Prices on
            # still-valid sides remain sticky so the market can reach them.
            self.entry_intents = tuple(previous.get(intent.side, intent) for intent in candidate)
        mode = "virtual-max" if cfg.virtual_max_mode else "dynamic"
        return StrategyDecision(
            self.entry_intents,
            features,
            False,
            f"sticky {mode} quote qty={qty} sizing={cfg.sizing_mode}",
        )


class IndependentFillSimulator:
    """Fill resting orders when a later real tick reaches their price and volume."""

    model_name = "independent"

    def __init__(self, latency_ms: float = 0.0) -> None:
        self.orders: dict[Side, SimulatedOrder] = {}
        self._sequence = 0
        self._volume = None
        self.latency_seconds = max(0.0, latency_ms) / 1000.0

    def sync(self, intents: Iterable[OrderIntent], now: float, quote: Quote | None = None) -> None:
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
                    f"SIM{self._sequence:08d}", intent, intent.quantity,
                    now + self.latency_seconds,
                )

    def on_quote(self, quote: Quote) -> list[Fill]:
        fills: list[Fill] = []
        if not quote.is_valid():
            return fills
        increment = max(0, quote.total_volume - self._volume) if self._volume is not None else 0
        self._volume = quote.total_volume
        available = min(max(0, quote.last_qty), int(increment))
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
            fills.append(Fill(side, order.intent.price, quantity, order.order_id, quote.received_at))
            order.remaining -= quantity
            available -= quantity
            if order.remaining <= 0:
                del self.orders[side]
            if available <= 0:
                break
        return fills


class ProbabilisticFillSimulator(IndependentFillSimulator):
    """Stress model: through-price ticks fill, at-price ticks fill randomly.

    The probability is deliberately explicit instead of being presented as an
    exchange fact.  Calibrate it from TTB order reports before using results for
    anything beyond sensitivity analysis.
    """

    model_name = "probabilistic"

    def __init__(
        self,
        touch_fill_probability: float = 0.35,
        latency_ms: float = 0.0,
        seed: int | None = None,
    ) -> None:
        super().__init__(latency_ms=latency_ms)
        if not 0.0 <= touch_fill_probability <= 1.0:
            raise ValueError("touch_fill_probability must be between 0 and 1")
        self.touch_fill_probability = touch_fill_probability
        self.random = random.Random(seed)

    def on_quote(self, quote: Quote) -> list[Fill]:
        fills: list[Fill] = []
        if not quote.is_valid():
            return fills
        increment = max(0, quote.total_volume - self._volume) if self._volume is not None else 0
        self._volume = quote.total_volume
        available = min(max(0, quote.last_qty), int(increment))
        if available <= 0:
            return fills
        for side in ("buy", "sell"):
            order = self.orders.get(side)
            if order is None or quote.received_at <= order.created_at:
                continue
            through = quote.last < order.intent.price if side == "buy" else quote.last > order.intent.price
            at_price = math.isclose(quote.last, order.intent.price, abs_tol=1e-9)
            if not through and not (at_price and self.random.random() < self.touch_fill_probability):
                continue
            quantity = min(order.remaining, available)
            if quantity <= 0:
                continue
            fills.append(Fill(side, order.intent.price, quantity, order.order_id, quote.received_at))
            order.remaining -= quantity
            available -= quantity
            if order.remaining <= 0:
                del self.orders[side]
            if available <= 0:
                break
        return fills


class QueueAwareFillSimulator(IndependentFillSimulator):
    """Conservative Level-1 queue approximation for robustness checks.

    A fraction of displayed best quantity is assumed to be ahead of a new
    order.  Same-price trade volume consumes that queue before it can fill the
    strategy.  A strict trade-through clears the estimated queue.  This is not
    a reconstruction of the real order book because TTB quote CSV contains
    only aggregate Level-1 data.
    """

    model_name = "queue"

    def __init__(self, queue_fraction: float = 1.0, latency_ms: float = 0.0) -> None:
        super().__init__(latency_ms=latency_ms)
        if queue_fraction < 0:
            raise ValueError("queue_fraction must be non-negative")
        self.queue_fraction = queue_fraction

    def sync(self, intents: Iterable[OrderIntent], now: float, quote: Quote | None = None) -> None:
        before = {side: order.order_id for side, order in self.orders.items()}
        super().sync(intents, now, quote)
        for side, order in self.orders.items():
            if before.get(side) == order.order_id or quote is None:
                continue
            visible = quote.bid_qty if side == "buy" else quote.ask_qty
            order.queue_ahead = max(0.0, visible * self.queue_fraction)

    def on_quote(self, quote: Quote) -> list[Fill]:
        fills: list[Fill] = []
        if not quote.is_valid():
            return fills
        increment = max(0, quote.total_volume - self._volume) if self._volume is not None else 0
        self._volume = quote.total_volume
        available: float = min(max(0, quote.last_qty), int(increment))
        if available <= 0:
            return fills
        for side in ("buy", "sell"):
            order = self.orders.get(side)
            if order is None or quote.received_at <= order.created_at:
                continue
            through = quote.last < order.intent.price if side == "buy" else quote.last > order.intent.price
            at_price = math.isclose(quote.last, order.intent.price, abs_tol=1e-9)
            if not through and not at_price:
                continue
            if through:
                order.queue_ahead = 0.0
            elif order.queue_ahead > 0:
                consumed = min(order.queue_ahead, available)
                order.queue_ahead -= consumed
                available -= consumed
                if available <= 0:
                    break
            quantity = min(order.remaining, math.floor(available))
            if quantity <= 0:
                continue
            fills.append(Fill(side, order.intent.price, quantity, order.order_id, quote.received_at))
            order.remaining -= quantity
            available -= quantity
            if order.remaining <= 0:
                del self.orders[side]
            if available <= 0:
                break
        return fills


def make_fill_simulator(
    model: FillModel = "independent",
    *,
    touch_fill_probability: float = 0.35,
    queue_fraction: float = 1.0,
    latency_ms: float = 0.0,
    seed: int | None = None,
) -> IndependentFillSimulator:
    """Build a named fill model used by replay and Monte Carlo research."""
    if model == "independent":
        return IndependentFillSimulator(latency_ms=latency_ms)
    if model == "probabilistic":
        return ProbabilisticFillSimulator(
            touch_fill_probability=touch_fill_probability,
            latency_ms=latency_ms,
            seed=seed,
        )
    if model == "queue":
        return QueueAwareFillSimulator(queue_fraction=queue_fraction, latency_ms=latency_ms)
    raise ValueError(f"unknown fill model: {model}")
