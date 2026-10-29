from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass

from strategy_core import FeatureEngine, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision

LOGGER = logging.getLogger(__name__)


def _seconds(text: str) -> float | None:
    try:
        hh, mm, ss = text.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (AttributeError, ValueError):
        return None


@dataclass(frozen=True)
class GridSettings:
    """Passive one-lot-per-level grid around an anchor price."""

    grid_points: float = 4.0
    exit_grids: int = 1
    levels: int = 3
    # Flatten once every level on a side is held and price runs this many
    # grids past the outermost one; negative disables the stop.
    stop_grids: float = 3.0
    reanchor_grids: float = 1.0
    first_entry_minute: int = 8 * 60 + 46
    last_entry_minute: int = 13 * 60 + 20
    flatten_minute: int = 13 * 60 + 35
    stop_slip_ticks: float = 2.0
    stop_ttl_seconds: float = 6.0

    def validate(self, config: StrategyConfig) -> None:
        if self.grid_points <= 0 or self.exit_grids < 1 or self.levels < 1 or self.reanchor_grids <= 0:
            raise ValueError("invalid grid geometry")
        ticks = self.grid_points / config.tick_size
        if abs(ticks - round(ticks)) > 1e-9:
            raise ValueError("grid_points must be a multiple of tick_size")
        if self.stop_grids >= 0 and self.levels + self.stop_grids < self.reanchor_grids:
            raise ValueError("a stop must leave price past the re-anchor distance")
        if self.levels * config.base_qty > config.max_inventory:
            raise ValueError("levels * base_qty exceeds max_inventory")
        if not (self.first_entry_minute < self.last_entry_minute < self.flatten_minute):
            raise ValueError("entry window must end before the flatten time")
        if self.stop_slip_ticks < 0 or self.stop_ttl_seconds <= 0:
            raise ValueError("invalid stop settings")


class GridPingPongStrategy:
    """Rest one lot at each grid level and take it off one grid higher.

    Winning TTB accounts made most of their profit this way on CDF: a resting
    buy one grid under the anchor, sold one grid higher, re-armed at the same
    price many times a day. Fills come top-down and exits bottom-up, so the
    held levels are always the nearest ones and the whole grid state is the
    anchor plus the net position. That needs only the next entry and the
    deepest level's exit, which fits the gateway's one order per side.
    """

    def __init__(self, config: StrategyConfig, ledger: PositionLedger, **settings) -> None:
        self.config = config
        self.ledger = ledger
        self.settings = GridSettings(**settings)
        self.settings.validate(config)
        self.features = FeatureEngine(config)
        self.anchor: float | None = None
        self.stopping = False
        self.risk_exit: OrderIntent | None = None
        self.risk_exit_at = 0.0

    def parameters(self) -> dict:
        return asdict(self.settings)

    def _snap(self, price: float) -> float:
        g = self.settings.grid_points
        return round(price / g) * g

    def _infer_anchor(self, position: int) -> float:
        # Held levels are anchor - g ... anchor - n*g (mirrored for shorts),
        # so their average sits (n + 1) / 2 grids from the anchor.
        g, q = self.settings.grid_points, self.config.base_qty
        held = math.ceil(abs(position) / q)
        sign = 1 if position > 0 else -1
        return self._snap(self.ledger.average_price + sign * (held + 1) * g / 2)

    def decide(self, quote: Quote) -> StrategyDecision:
        features = self.features.update(quote)
        if not quote.is_valid() or quote.symbol.upper() != self.config.symbol.upper():
            return StrategyDecision((), features, True, "invalid quote")
        seconds = _seconds(quote.exchange_time)
        if seconds is None:
            return StrategyDecision((), features, False, "grid: no exchange time")
        s, tick, now = self.settings, self.config.tick_size, quote.received_at
        g, q = s.grid_points, self.config.base_qty
        pos = self.ledger.position
        if self.anchor is None:
            self.anchor = self._snap(quote.last) if pos == 0 else self._infer_anchor(pos)
            LOGGER.info("grid anchor %.2f (position %s)", self.anchor, pos)
        held = math.ceil(abs(pos) / q)
        sign = 1 if pos > 0 else -1

        if pos == 0:
            self.stopping = False
            self.risk_exit = None
        else:
            outer = self.anchor - sign * (s.levels + s.stop_grids) * g
            if s.stop_grids >= 0 and held >= s.levels and sign * (quote.last - outer) <= 0:
                # Stay in the stop until flat: after a partial fill the
                # remaining levels must not re-arm entries above the market.
                self.stopping = True
            if self.stopping or seconds >= s.flatten_minute * 60:
                reason = "grid stop" if self.stopping else "grid flatten"
                current = self.risk_exit
                if current is None or current.quantity != abs(pos) or now - self.risk_exit_at >= s.stop_ttl_seconds:
                    price = quote.bid - s.stop_slip_ticks * tick if pos > 0 else quote.ask + s.stop_slip_ticks * tick
                    current = OrderIntent("sell" if pos > 0 else "buy", price, abs(pos), "risk_exit", reason)
                    self.risk_exit, self.risk_exit_at = current, now
                # Once flat, price is past the re-anchor distance, so the grid
                # restarts around it.
                return StrategyDecision((current,), features, False, reason)

        if pos == 0 and abs(quote.last - self.anchor) >= s.reanchor_grids * g:
            self.anchor = self._snap(quote.last)
        entries_open = s.first_entry_minute * 60 <= seconds < s.last_entry_minute * 60
        intents = []
        if pos == 0:
            if entries_open:
                intents.append(OrderIntent("buy", self.anchor - g, q, "entry", "grid level 1"))
                intents.append(OrderIntent("sell", self.anchor + g, q, "entry", "grid level 1"))
        else:
            deepest = self.anchor - sign * held * g
            exit_qty = abs(pos) - (held - 1) * q
            intents.append(OrderIntent("sell" if pos > 0 else "buy", deepest + sign * s.exit_grids * g,
                                       exit_qty, "exit", f"grid exit level {held}"))
            if entries_open and held < s.levels:
                intents.append(OrderIntent("buy" if pos > 0 else "sell", deepest - sign * g, q,
                                           "entry", f"grid level {held + 1}"))
        return StrategyDecision(tuple(intents), features, False, f"grid anchor {self.anchor:g} held {held}")
