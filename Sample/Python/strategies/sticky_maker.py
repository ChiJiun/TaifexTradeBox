from __future__ import annotations

from dataclasses import dataclass, asdict

from strategy_core import MicropriceMarketMaker, PositionLedger, StrategyConfig


@dataclass(frozen=True)
class StickyMakerSettings:
    """Serializable strategy-specific settings; the base parameters live in StrategyConfig."""
    require_positive_one_tick_edge: bool = True


class StickyOneTickMaker(MicropriceMarketMaker):
    """Executable sticky passive maker. Never places orders directly.

    One-tick targets are permitted only when projected round-trip costs are
    covered; the gateway still enforces all order/portfolio limits.
    """

    def __init__(
        self, config: StrategyConfig, ledger: PositionLedger,
        require_positive_one_tick_edge: bool = True,
    ) -> None:
        super().__init__(config, ledger)
        self.sticky_settings = StickyMakerSettings(require_positive_one_tick_edge)

    def decide(self, quote):
        decision = super().decide(quote)
        if (self.sticky_settings.require_positive_one_tick_edge
            and self.ledger.position == 0
            and self.config.target_ticks == 1
            and self.config.tick_value <= self.config.estimated_round_trip_cost(quote.mid)
            and any(intent.role == "entry" for intent in decision.intents)):
            self._clear_entry_quote()
            return type(decision)((), decision.features, False, "one-tick cycle fails cost floor")
        return decision

    def parameters(self) -> dict:
        return asdict(self.sticky_settings)
