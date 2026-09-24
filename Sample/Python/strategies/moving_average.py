from __future__ import annotations

from collections import deque

from strategy_core import FeatureEngine, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision


class MovingAverageStrategy:
    """Minimal example showing how a strategy calls the shared order layer.

    It emits ``OrderIntent`` objects instead of calling ``NEWORDER`` directly.
    The engine sends them to either ``ShadowGateway`` or ``TTBOrderGateway``.
    This keeps exactly the same strategy usable in simulation and virtual TTB.
    """

    def __init__(
        self,
        config: StrategyConfig,
        ledger: PositionLedger,
        short_window: int = 5,
        long_window: int = 20,
        entry_ticks: float = 0.5,
    ) -> None:
        if short_window < 1 or long_window <= short_window:
            raise ValueError("long_window must be greater than short_window")
        if entry_ticks < 0:
            raise ValueError("entry_ticks must be non-negative")
        self.config = config
        self.ledger = ledger
        self.short_window = short_window
        self.long_window = long_window
        self.entry_ticks = entry_ticks
        self.prices: deque[float] = deque(maxlen=long_window)
        self.features = FeatureEngine(config)

    def decide(self, quote: Quote) -> StrategyDecision:
        features = self.features.update(quote)
        if not quote.is_valid() or quote.symbol.upper() != self.config.symbol.upper():
            return StrategyDecision((), features, True, "invalid quote")
        self.prices.append(quote.mid)
        if len(self.prices) < self.long_window:
            return StrategyDecision((), features, False, "moving-average warmup")

        position = self.ledger.position
        if position:
            side = "sell" if position > 0 else "buy"
            sign = 1 if position > 0 else -1
            target = self.ledger.average_price + sign * self.config.target_ticks * self.config.tick_size
            stop = self.ledger.average_price - sign * self.config.stop_ticks * self.config.tick_size
            stop_hit = quote.last <= stop if position > 0 else quote.last >= stop
            price = (quote.bid if position > 0 else quote.ask) if stop_hit else target
            role = "risk_exit" if stop_hit else "exit"
            return StrategyDecision(
                (OrderIntent(side, price, abs(position), role, "moving-average position exit"),),
                features,
                False,
                "manage moving-average position",
            )

        values = list(self.prices)
        short_average = sum(values[-self.short_window:]) / self.short_window
        long_average = sum(values) / self.long_window
        threshold = self.entry_ticks * self.config.tick_size
        if short_average >= long_average + threshold:
            intent = OrderIntent("buy", quote.bid, self.config.base_qty, "entry", "short average above long average")
            return StrategyDecision((intent,), features, False, "moving-average long")
        if short_average <= long_average - threshold:
            intent = OrderIntent("sell", quote.ask, self.config.base_qty, "entry", "short average below long average")
            return StrategyDecision((intent,), features, False, "moving-average short")
        return StrategyDecision((), features, False, "moving averages neutral")
