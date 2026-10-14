from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from strategy_core import PositionLedger, Quote, StrategyConfig, StrategyDecision
from .sticky_maker import StickyOneTickMaker


@dataclass(frozen=True)
class FlowGateSettings:
    entry_threshold: float = 0.18
    high_volatility_ratio: float = 2.5
    short_window: int = 8
    long_window: int = 40

    def validate(self) -> None:
        if not (0 <= self.entry_threshold <= 1
                and self.high_volatility_ratio > 1
                and 2 <= self.short_window < self.long_window):
            raise ValueError("invalid order-flow filter settings")


class OrderFlowGatedMaker(StickyOneTickMaker):
    """Filter existing maker intents using *current/past* order flow only.

    Risk exits are never filtered. The gateway retains independent authority
    over inventory, margin, owner IDs and daily limits.
    """

    def __init__(
        self, config: StrategyConfig, ledger: PositionLedger,
        entry_threshold: float = 0.18,
        high_volatility_ratio: float = 2.5,
        short_window: int = 8,
        long_window: int = 40,
    ) -> None:
        super().__init__(config, ledger, require_positive_one_tick_edge=False)
        self.settings = FlowGateSettings(
            entry_threshold, high_volatility_ratio, short_window, long_window,
        )
        self.settings.validate()

    @staticmethod
    def _std(values: list[float]) -> float:
        avg = sum(values) / len(values)
        return math.sqrt(sum((x - avg) ** 2 for x in values) / len(values))

    def decide(self, quote: Quote) -> StrategyDecision:
        decision = super().decide(quote)
        if decision.halted or self.ledger.position or not decision.intents:
            return decision
        # Pre-existing long-window requirement avoids trading on an unstable
        # estimated regime, without inspecting any future tick.
        mids = [price for _, price in self.history]
        if len(mids) < self.settings.long_window:
            self._clear_entry_quote()
            return StrategyDecision((), decision.features, False, "flow regime warmup")
        long_std = self._std(mids[-self.settings.long_window:])
        short_std = self._std(mids[-self.settings.short_window:])
        if (long_std > 0 and short_std / long_std >= self.settings.high_volatility_ratio):
            self._clear_entry_quote()
            return StrategyDecision((), decision.features, False, "flow high volatility gate")
        z = decision.features.score
        theta = self.settings.entry_threshold
        allowed = (
            {"buy"} if z >= theta else
            {"sell"} if z <= -theta else
            {"buy", "sell"}
        )
        filtered = tuple(
            intent for intent in decision.intents
            if intent.role != "entry" or intent.side in allowed
        )
        self.entry_intents = tuple(x for x in self.entry_intents if x.side in allowed)
        return StrategyDecision(filtered, decision.features, decision.halted,
                                "flow-gated maker: " + decision.reason)

    def parameters(self) -> dict:
        return asdict(self.settings)
