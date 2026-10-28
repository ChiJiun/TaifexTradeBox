from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from strategy_core import OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision
from .sticky_maker import StickyOneTickMaker


@dataclass(frozen=True)
class PnlShapingSettings:
    previous_day_equity: float | None = None
    daily_profit_fraction: float = 0.002
    daily_loss_fraction: float = 0.01
    daily_drawdown_fraction: float = 0.008

    def validate(self) -> None:
        if self.previous_day_equity is not None and (
            not math.isfinite(self.previous_day_equity) or self.previous_day_equity <= 0
        ):
            raise ValueError("previous_day_equity must be positive")
        if not all(0 < v < 1 for v in (
            self.daily_profit_fraction, self.daily_loss_fraction,
            self.daily_drawdown_fraction,
        )):
            raise ValueError("daily fraction limits must be between 0 and 1")


class CompetitionPnlShaping(StickyOneTickMaker):
    """Daily conservative equity-based stop/target wrapper.

    Requires operator-verified previous-day equity for new entries. Halts are
    persistent through engine RiskStateStore's halt_reason. Never auto-scales.
    """

    def __init__(
        self, config: StrategyConfig, ledger: PositionLedger,
        previous_day_equity: float | None = None,
        daily_profit_fraction: float = 0.002,
        daily_loss_fraction: float = 0.01,
        daily_drawdown_fraction: float = 0.008,
        **kwargs,
    ) -> None:
        kwargs.setdefault("require_positive_one_tick_edge", False)
        super().__init__(config, ledger, **kwargs)
        self.pnl_settings = PnlShapingSettings(
            previous_day_equity, daily_profit_fraction,
            daily_loss_fraction, daily_drawdown_fraction,
        )
        self.pnl_settings.validate()
        self._pnl_peak = 0.0

    def decide(self, quote: Quote) -> StrategyDecision:
        if not quote.is_valid():
            return super().decide(quote)
        pos = self.ledger.position
        liquidation = quote.bid if pos > 0 else quote.ask
        conservative = self.ledger.mark_to_market(liquidation)
        conservative -= abs(pos) * (
            self.config.fee_per_fill
            + abs(liquidation * self.config.point_value * self.config.tax_rate)
            + self.config.slippage_ticks * self.config.tick_value
        )
        self._pnl_peak = max(self._pnl_peak, conservative)
        if self.pnl_settings.previous_day_equity is None:
            # No unverified account-equity assumptions. Reductions continue.
            if not self.halt_reason:
                self.halt_reason = "previous_day_equity_unverified"
        elif not self.halt_reason:
            # First triggered reason remains latched for the entire trading
            # day, including across strategy ticks and risk-state restarts.
            equity = self.pnl_settings.previous_day_equity
            if conservative <= -equity * self.pnl_settings.daily_loss_fraction:
                self.halt_reason = "daily_equity_loss_limit"
            elif self._pnl_peak - conservative >= equity * self.pnl_settings.daily_drawdown_fraction:
                self.halt_reason = "daily_equity_drawdown"
            elif conservative >= equity * self.pnl_settings.daily_profit_fraction:
                self.halt_reason = "daily_profit_target"
        result = super().decide(quote)
        if self.halt_reason and not pos:
            self._clear_entry_quote()
            return StrategyDecision((), result.features, True, self.halt_reason)
        return result

    def parameters(self) -> dict:
        return {**super().parameters(), **asdict(self.pnl_settings)}
