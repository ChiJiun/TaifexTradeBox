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
    # Flatten once price runs this many grids past the outermost level, held
    # or not; negative disables the stop.
    stop_grids: float = 3.0
    reanchor_grids: float = 1.0
    first_entry_minute: int = 8 * 60 + 46
    last_entry_minute: int = 13 * 60 + 20
    flatten_minute: int = 13 * 60 + 35
    stop_slip_ticks: float = 2.0
    stop_ttl_seconds: float = 6.0
    # Size every level from the account: margin_share of equity buys
    # levels x qty lots at margin_rate of the current contract value, so the
    # grid grows with equity and shrinks as the price rises. 0 keeps base_qty.
    margin_share: float = 0.0
    # Or size it in steps: ((equity floor, lots per level), ...), the highest
    # floor the equity reaches wins. In replay two CCF lots, where
    # margin_share lands at 2M, earned less than one and left the least spare
    # margin; three beat one only from 3M up.
    qty_tiers: tuple = ()
    # Size the level count the same way: level_share of equity buys up to
    # `levels` levels of base_qty lots. A stock future fills against informed
    # flow when a level carries more lots, so deeper grids scale it better.
    level_share: float = 0.0
    margin_rate: float = 0.162
    equity: float = 0.0

    def validate(self, config: StrategyConfig) -> None:
        if self.grid_points <= 0 or self.exit_grids < 1 or self.levels < 1:
            raise ValueError("invalid grid geometry")
        if not 0 < self.reanchor_grids <= 1:
            # Farther re-anchoring leaves a flat grid's first level on the
            # wrong side of the market.
            raise ValueError("reanchor_grids must be in (0, 1]")
        ticks = self.grid_points / config.tick_size
        if abs(ticks - round(ticks)) > 1e-9:
            raise ValueError("grid_points must be a multiple of tick_size")
        if self.levels * config.base_qty > config.max_inventory:
            raise ValueError("levels * base_qty exceeds max_inventory")
        if not (self.first_entry_minute < self.last_entry_minute < self.flatten_minute):
            raise ValueError("entry window must end before the flatten time")
        if self.stop_slip_ticks < 0 or self.stop_ttl_seconds <= 0:
            raise ValueError("invalid stop settings")
        if not (0 <= self.margin_share <= 1 and 0 <= self.level_share <= 1):
            raise ValueError("margin_share and level_share must be in [0, 1]")
        if self.margin_share and self.level_share:
            # Each share is a fraction of equity on its own; together they
            # would hold neither.
            raise ValueError("size either the lots per level or the level count, not both")
        if (self.margin_share or self.level_share) and (self.equity <= 0 or self.margin_rate <= 0):
            raise ValueError("margin sizing needs a positive equity and margin_rate")
        if self.qty_tiers:
            if self.margin_share:
                raise ValueError("size the lots per level by margin_share or qty_tiers, not both")
            if self.equity <= 0:
                raise ValueError("qty_tiers needs a positive equity")
            try:
                floors, lots = zip(*[(floor, qty) for floor, qty in self.qty_tiers])
            except (TypeError, ValueError):
                raise ValueError("qty_tiers must be [[equity floor, lots per level], ...]") from None
            if any(not isinstance(f, (int, float)) or isinstance(f, bool) for f in floors) or any(
                    b <= a for a, b in zip(floors, floors[1:])):
                raise ValueError("qty_tiers equity floors must be numbers that increase")
            if any(not isinstance(q, int) or isinstance(q, bool) or q < 1 for q in lots):
                raise ValueError("qty_tiers lots per level must be integers >= 1")


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
        self.last_position = 0
        self.stopping = False
        self.risk_exit: OrderIntent | None = None
        self.risk_exit_at = 0.0
        self.qty = config.base_qty
        self.levels = self.settings.levels
        self.sized = False
        self.levels_sized = False

    def parameters(self) -> dict:
        return asdict(self.settings)

    def _snap(self, price: float) -> float:
        g = self.settings.grid_points
        return round(price / g) * g

    def _size_levels(self, price: float) -> None:
        s = self.settings
        if not s.level_share or price <= 0 or self.levels_sized:
            return
        self.levels_sized = True
        level_margin = price * self.config.point_value * s.margin_rate * self.config.base_qty
        self.levels = max(1, min(s.levels, int(s.level_share * s.equity // level_margin)))

    def _size(self, price: float) -> None:
        s = self.settings
        if not (s.margin_share or s.qty_tiers) or price <= 0 or self.sized:
            return
        self.sized = True
        cap = max(1, min(self.config.max_order_qty, self.config.max_inventory // self.levels))
        if s.qty_tiers:
            # Below the lowest floor the grid keeps base_qty.
            qty = next((q for floor, q in reversed(s.qty_tiers) if s.equity >= floor), self.config.base_qty)
        else:
            lot_margin = price * self.config.point_value * s.margin_rate
            qty = int(s.margin_share * s.equity // (self.levels * lot_margin))
        self.qty = max(1, min(cap, qty))

    def _infer_anchor(self, position: int) -> float:
        # Held levels are anchor - g ... anchor - n*g (mirrored for shorts),
        # so their average sits (n + 1) / 2 grids from the anchor.
        g, q = self.settings.grid_points, self.qty
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
        pos = self.ledger.position
        # The level count only moves the stop line and the deepest entry, so
        # it can be sized on a restart that already holds inventory.
        self._size_levels(quote.last)
        if pos == 0:
            # Sized once, from the first flat price: held levels stay uniform
            # and a price near a lot boundary never re-quotes the grid.
            self._size(quote.last)
        g, q = s.grid_points, self.qty
        if self.anchor is None:
            self.anchor = self._snap(quote.last) if pos == 0 else self._infer_anchor(pos)
            LOGGER.info("grid anchor %.2f (position %s)", self.anchor, pos)
        if pos * self.last_position <= 0 and pos != 0:
            # Fills reach the ledger a poll late, and a flat re-anchor in the
            # meantime may have moved the anchor onto the fill price. Rebuild
            # it from the fills so the exit stays one grid away.
            self.anchor = self._infer_anchor(pos)
        self.last_position = pos
        held = math.ceil(abs(pos) / q)
        sign = 1 if pos > 0 else -1

        if pos == 0:
            self.stopping = False
            self.risk_exit = None
        else:
            outer = self.anchor - sign * (self.levels + s.stop_grids) * g
            # A gap can cross the stop line before every level fills; stop on
            # the line itself so the grid never buys its way down to it.
            if s.stop_grids >= 0 and sign * (quote.last - outer) <= 0:
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
            if entries_open and held < self.levels:
                intents.append(OrderIntent("buy" if pos > 0 else "sell", deepest - sign * g, q,
                                           "entry", f"grid level {held + 1}"))
        # An entry already through the market would fill at once above (below)
        # its level; skip it until price comes back to the level. An entry that
        # only touches the best opposite quote fills at its own level and stays:
        # skipping it froze a CCF grid for 50 minutes on 2026-10-30 while the
        # bid sat on the next sell level.
        intents = [i for i in intents if i.role != "entry" or (
            i.price <= quote.last and i.price <= quote.ask if i.side == "buy" else i.price >= quote.last and i.price >= quote.bid)]
        return StrategyDecision(tuple(intents), features, False, f"grid anchor {self.anchor:g} held {held}")
