from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

from strategy_core import FeatureEngine, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision


@dataclass(frozen=True)
class FlowMomentumSettings:
    """Price-burst trigger confirmed by tick-rule flow; exits by target, stop or time."""

    window_seconds: float = 30.0
    flow_threshold: float = 50.0
    move_ticks: float = 5.0
    big_trade_qty: float = 0.0
    follow: bool = True
    target_ticks: float = 30.0
    stop_ticks: float = 25.0
    max_hold_seconds: float = 180.0
    cooldown_seconds: float = 30.0
    entry_slip_ticks: float = 2.0
    entry_ttl_seconds: float = 6.0
    previous_day_equity: float | None = None
    daily_loss_fraction: float = 0.01
    daily_drawdown_fraction: float = 0.008

    def validate(self) -> None:
        if self.window_seconds <= 0 or self.flow_threshold < 0 or self.move_ticks < 0 or self.big_trade_qty < 0:
            raise ValueError("invalid flow trigger settings")
        if self.target_ticks <= 0 or self.stop_ticks <= 0 or self.max_hold_seconds <= 0:
            raise ValueError("target, stop and max hold must be positive")
        if self.entry_slip_ticks < 0 or self.entry_ttl_seconds <= 0 or self.cooldown_seconds < 0:
            raise ValueError("invalid entry settings")
        if self.previous_day_equity is not None and self.previous_day_equity <= 0:
            raise ValueError("previous_day_equity must be positive")
        if not (0 < self.daily_loss_fraction < 1 and 0 < self.daily_drawdown_fraction < 1):
            raise ValueError("daily fractions must be between 0 and 1")


class FlowMomentumStrategy:
    """Trade in (or against) the direction of a fast move and its volume.

    TTB matches every order independently against real prints, so a resting
    target fills on the first print at its price and a marketable order fills
    at the next print with no market impact. On 29 TAIFEX day sessions of TXF
    (2026-09-11 to 10-28) following an 80-point move inside 45 seconds and
    leaving at +100 / -60 points or after 60 seconds was profitable in both
    halves of the sample and across neighbouring parameters.
    """

    def __init__(self, config: StrategyConfig, ledger: PositionLedger, **settings) -> None:
        self.config = config
        self.ledger = ledger
        self.settings = FlowMomentumSettings(**settings)
        self.settings.validate()
        self.features = FeatureEngine(config)
        self.flow: deque[tuple[float, float, float]] = deque()
        self.previous: Quote | None = None
        self.last_trade: float | None = None
        self.tick_sign = 0
        self.entry: OrderIntent | None = None
        self.entry_at = 0.0
        self.risk_exit: OrderIntent | None = None
        self.risk_exit_at = 0.0
        self.flat_since = 0.0
        self.peak_pnl = 0.0
        self.halt_reason = ""

    def parameters(self) -> dict:
        return asdict(self.settings)

    def _aggressor_qty(self, quote: Quote) -> float:
        # Tick rule, so live trading signs volume the same way as a backtest
        # on TAIFEX prints, which carry no bid/ask: an uptick is buying and a
        # zero tick keeps the last direction.
        prev = self.previous
        if prev is None or quote.total_volume <= prev.total_volume:
            return 0.0
        if self.last_trade is not None and quote.last != self.last_trade:
            self.tick_sign = 1 if quote.last > self.last_trade else -1
        self.last_trade = quote.last
        return self.tick_sign * (quote.total_volume - prev.total_volume)

    def _daily_halt(self, quote: Quote) -> str:
        if self.halt_reason:
            return self.halt_reason
        s = self.settings
        if s.previous_day_equity is None:
            return ""
        pos = self.ledger.position
        liquidation = quote.bid if pos > 0 else quote.ask
        pnl = self.ledger.mark_to_market(liquidation) - abs(pos) * (
            self.config.fee_per_fill + liquidation * self.config.point_value * self.config.tax_rate
        )
        self.peak_pnl = max(self.peak_pnl, pnl)
        if pnl <= -s.previous_day_equity * s.daily_loss_fraction:
            self.halt_reason = "daily_equity_loss_limit"
        elif self.peak_pnl - pnl >= s.previous_day_equity * s.daily_drawdown_fraction:
            self.halt_reason = "daily_equity_drawdown"
        return self.halt_reason

    def decide(self, quote: Quote) -> StrategyDecision:
        features = self.features.update(quote)
        if not quote.is_valid() or quote.symbol.upper() != self.config.symbol.upper():
            return StrategyDecision((), features, True, "invalid quote")
        s, tick, now = self.settings, self.config.tick_size, quote.received_at
        if self.last_trade is None:
            self.last_trade = quote.last
        self.flow.append((now, self._aggressor_qty(quote), quote.last))
        while self.flow and self.flow[0][0] < now - s.window_seconds:
            self.flow.popleft()
        big = max((q for _, q, _ in self.flow), key=abs, default=0.0)
        net_flow = sum(q for _, q, _ in self.flow)
        move = (quote.last - self.flow[0][2]) / tick
        self.previous = quote
        halt = self._daily_halt(quote)

        pos = self.ledger.position
        if not pos:
            self.risk_exit = None
        if pos:
            self.entry = None
            self.flat_since = 0.0
            sign = 1 if pos > 0 else -1
            side = "sell" if pos > 0 else "buy"
            exit_price = quote.bid if pos > 0 else quote.ask
            adverse = sign * (exit_price - self.ledger.average_price) / tick
            held = now - self.ledger.opened_at if self.ledger.opened_at else 0.0
            if self.risk_exit and now - self.risk_exit_at < s.entry_ttl_seconds:
                return StrategyDecision((self.risk_exit,), features, bool(halt), "flow exit pending")
            if halt or adverse <= -s.stop_ticks or held >= s.max_hold_seconds:
                # Hold the crossing price steady so the order can reach TTB and fill.
                price = exit_price - sign * s.entry_slip_ticks * tick
                reason = halt or ("stop" if adverse <= -s.stop_ticks else "max hold")
                self.risk_exit = OrderIntent(side, price, abs(pos), "risk_exit", reason)
                self.risk_exit_at = now
                return StrategyDecision((self.risk_exit,), features, bool(halt), "flow exit: " + reason)
            target = self.ledger.average_price + sign * s.target_ticks * tick
            return StrategyDecision((OrderIntent(side, target, abs(pos), "exit", "flow target"),),
                                    features, False, "flow manage")

        if not self.flat_since:
            self.flat_since = now
        if halt:
            self.entry = None
            return StrategyDecision((), features, True, halt)
        if self.entry and now - self.entry_at < s.entry_ttl_seconds:
            return StrategyDecision((self.entry,), features, False, "flow entry pending")
        self.entry = None
        if now - self.flat_since < s.cooldown_seconds:
            return StrategyDecision((), features, False, "flow idle")
        direction = 0
        if s.big_trade_qty and abs(big) >= s.big_trade_qty and (big > 0 and move >= s.move_ticks or big < 0 and move <= -s.move_ticks):
            direction = 1 if big > 0 else -1
        elif net_flow >= s.flow_threshold and move >= s.move_ticks:
            direction = 1
        elif net_flow <= -s.flow_threshold and move <= -s.move_ticks:
            direction = -1
        if not s.follow:
            direction = -direction
        if not direction:
            return StrategyDecision((), features, False, "flow quiet")
        side = "buy" if direction > 0 else "sell"
        price = quote.ask + s.entry_slip_ticks * tick if direction > 0 else quote.bid - s.entry_slip_ticks * tick
        self.entry = OrderIntent(side, price, self.config.base_qty, "entry", "flow signal")
        self.entry_at = now
        self.flow.clear()
        return StrategyDecision((self.entry,), features, False, f"flow {side} flow={net_flow:+.0f} move={move:+.0f}")
