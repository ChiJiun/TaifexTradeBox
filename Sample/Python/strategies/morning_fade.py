from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy_core import FeatureEngine, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision

LOGGER = logging.getLogger(__name__)
# Fixed offset: Windows Python has no tz database without the tzdata package.
TAIPEI = timezone(timedelta(hours=8))
OPEN_SECONDS = 8 * 3600 + 45 * 60
REPLACE_ATTEMPTS = 5


def _seconds(text: str) -> float | None:
    try:
        hh, mm, ss = text.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (AttributeError, ValueError):
        return None


@dataclass(frozen=True)
class MorningFadeSettings:
    """One trade a day against the move from the open to the signal time."""

    signal_minute: int = 600  # 10:00 Taipei
    entry_window_minutes: float = 5.0
    # The research used the 08:45 print; a much later first quote is a
    # different signal, so skip the day instead.
    latest_open_minute: int = 527  # 08:47
    threshold_ticks: float = 0.0
    stop_ticks: float = 150.0
    entry_slip_ticks: float = 2.0
    entry_ttl_seconds: float = 6.0
    state_path: str | None = None

    def validate(self) -> None:
        if not (OPEN_SECONDS / 60 < self.signal_minute < 13 * 60 + 20):
            raise ValueError("signal_minute must fall inside the day session")
        if not (OPEN_SECONDS / 60 <= self.latest_open_minute < self.signal_minute):
            raise ValueError("latest_open_minute must fall between the open and the signal")
        if self.entry_window_minutes <= 0 or self.threshold_ticks < 0 or self.stop_ticks <= 0:
            raise ValueError("invalid window, threshold or stop")
        if self.entry_slip_ticks < 0 or self.entry_ttl_seconds <= 0:
            raise ValueError("invalid entry settings")


class MorningFadeStrategy:
    """Fade the morning move and hold it to the engine's close flatten.

    On 29 TAIFEX day sessions (2026-09-11 to 10-28) the move from the 08:45
    open to 10:00 tended to retrace by the close; one contract a day keeps the
    12-point round-trip cost small against an intraday swing. The engine
    flattens ten minutes before the close, so the strategy only enters and
    stops out. The session open and whether today's entry was already sent
    are persisted so a bot restart neither loses the signal nor re-enters.
    """

    def __init__(self, config: StrategyConfig, ledger: PositionLedger, **settings) -> None:
        self.config = config
        self.ledger = ledger
        self.settings = MorningFadeSettings(**settings)
        self.settings.validate()
        self.features = FeatureEngine(config)
        self.state_path = Path(self.settings.state_path) if self.settings.state_path else None
        self.day = ""
        self.dirty = False
        self._roll_day()

    def parameters(self) -> dict:
        return asdict(self.settings)

    def _today(self) -> str:
        return datetime.now(TAIPEI).date().isoformat()

    def _roll_day(self) -> None:
        today = self._today()
        if today == self.day:
            return
        self.day = today
        self.open_price: float | None = None
        self.open_time = ""
        self.traded = False
        self.entry: OrderIntent | None = None
        self.entry_at = 0.0
        self.risk_exit: OrderIntent | None = None
        self.risk_exit_at = 0.0
        if not self.state_path:
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if state.get("day") == today:
            self.open_price = state.get("open_price")
            self.open_time = state.get("open_time", "")
            self.traded = bool(state.get("traded"))

    def _save_state(self) -> None:
        self.dirty = True
        if not self.state_path:
            self.dirty = False
            return
        state = {"day": self.day, "open_price": self.open_price, "open_time": self.open_time, "traded": self.traded}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            # A reader or scanner holding the file makes the replace fail on
            # Windows. Memory already holds the right state, and stopping the
            # bot over it would force exactly the restart this file guards.
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.state_path)
                    self.dirty = False
                    return
                except PermissionError:
                    if attempt + 1 < REPLACE_ATTEMPTS:
                        time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            LOGGER.warning("morning fade state %s not saved: %s", self.state_path, exc)
            return
        LOGGER.warning("morning fade state %s locked; retrying on the next quote", self.state_path)

    def decide(self, quote: Quote) -> StrategyDecision:
        features = self.features.update(quote)
        if not quote.is_valid() or quote.symbol.upper() != self.config.symbol.upper():
            return StrategyDecision((), features, True, "invalid quote")
        self._roll_day()
        if self.dirty:
            self._save_state()
        s, tick, now = self.settings, self.config.tick_size, quote.received_at
        seconds = _seconds(quote.exchange_time)
        if seconds is None:
            return StrategyDecision((), features, False, "fade: no exchange time")
        if self.open_price is None and OPEN_SECONDS <= seconds <= s.latest_open_minute * 60:
            self.open_price = quote.last
            self.open_time = quote.exchange_time
            LOGGER.info("morning fade open %.2f at %s", self.open_price, self.open_time)
            self._save_state()

        pos = self.ledger.position
        if pos:
            self.entry = None
            if not self.traded:
                self.traded = True
                self._save_state()
            if self.risk_exit and now - self.risk_exit_at < s.entry_ttl_seconds:
                return StrategyDecision((self.risk_exit,), features, False, "fade stop pending")
            sign = 1 if pos > 0 else -1
            exit_price = quote.bid if pos > 0 else quote.ask
            if sign * (exit_price - self.ledger.average_price) / tick <= -s.stop_ticks:
                # Hold the crossing price steady so the order can reach TTB and fill.
                price = exit_price - sign * s.entry_slip_ticks * tick
                self.risk_exit = OrderIntent("sell" if pos > 0 else "buy", price, abs(pos), "risk_exit", "fade stop")
                self.risk_exit_at = now
                return StrategyDecision((self.risk_exit,), features, False, "fade stop")
            return StrategyDecision((), features, False, "fade hold to close")

        self.risk_exit = None
        start = s.signal_minute * 60
        in_window = start <= seconds < start + s.entry_window_minutes * 60
        if self.entry is not None:
            # Today's entry is in flight: keep its side, refresh only the price.
            if not in_window:
                self.entry = None
                return StrategyDecision((), features, False, "fade entry expired")
            if now - self.entry_at < s.entry_ttl_seconds:
                return StrategyDecision((self.entry,), features, False, "fade entry pending")
            side = self.entry.side
        elif self.traded or self.open_price is None:
            return StrategyDecision((), features, False, "fade done" if self.traded else "fade: open not seen")
        elif not in_window:
            return StrategyDecision((), features, False, "fade waiting")
        else:
            move = (quote.last - self.open_price) / tick
            # Persist before the first order goes out: a restart after a fill
            # must not enter a second time.
            self.traded = True
            self._save_state()
            if abs(move) <= s.threshold_ticks or move == 0:
                return StrategyDecision((), features, False, f"fade skip move={move:+.0f}")
            side = "sell" if move > 0 else "buy"
        price = quote.bid - s.entry_slip_ticks * tick if side == "sell" else quote.ask + s.entry_slip_ticks * tick
        self.entry = OrderIntent(side, price, self.config.base_qty, "entry", "morning fade")
        self.entry_at = now
        return StrategyDecision((self.entry,), features, False, f"fade {side} vs open {self.open_price:.0f}")
