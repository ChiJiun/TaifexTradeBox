from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal


TAIPEI_TZ = timezone(timedelta(hours=8))
SessionPhase = Literal["regular", "after_hours", "closed"]


def _parse_clock(value: str) -> int:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid HH:MM clock: {value!r}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid HH:MM clock: {value!r}")
    return hour * 60 + minute


@dataclass(frozen=True)
class SessionStatus:
    phase: SessionPhase
    trading_day: str
    minutes_since_open: float
    minutes_until_close: float

    @property
    def trading(self) -> bool:
        return self.phase != "closed"


@dataclass(frozen=True)
class SessionSchedule:
    """Configurable TAIFEX-style regular and overnight trading windows.

    This is a clock schedule, not an exchange holiday calendar. A missing first
    quote is therefore treated as "awaiting market" by the engine rather than
    immediately declaring stale data. Contract-specific expiry-day close times
    can be overridden with the CLI clock options.
    """

    regular_open: str = "08:45"
    regular_close: str = "13:45"
    after_hours_open: str = "15:00"
    after_hours_close: str = "05:00"
    include_after_hours: bool = True

    def __post_init__(self) -> None:
        ro = _parse_clock(self.regular_open)
        rc = _parse_clock(self.regular_close)
        ao = _parse_clock(self.after_hours_open)
        ac = _parse_clock(self.after_hours_close)
        if ro >= rc:
            raise ValueError("regular session must open before it closes")
        if self.include_after_hours and ao <= ac:
            raise ValueError("after-hours session must cross midnight")

    @staticmethod
    def _next_business_day(value: datetime) -> str:
        candidate = value.date() + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate.isoformat()

    def status(self, local: datetime | None = None) -> SessionStatus:
        local = local or datetime.now(TAIPEI_TZ)
        if local.tzinfo is None:
            local = local.replace(tzinfo=TAIPEI_TZ)
        else:
            local = local.astimezone(TAIPEI_TZ)

        minute = local.hour * 60 + local.minute + local.second / 60.0
        weekday = local.weekday()
        ro = _parse_clock(self.regular_open)
        rc = _parse_clock(self.regular_close)
        ao = _parse_clock(self.after_hours_open)
        ac = _parse_clock(self.after_hours_close)

        if weekday < 5 and ro <= minute < rc:
            return SessionStatus(
                "regular",
                local.date().isoformat(),
                minute - ro,
                rc - minute,
            )

        if self.include_after_hours:
            # The afternoon/evening session is grouped with the next regular
            # business day for risk accounting (e.g. Monday 15:00 -> Tuesday).
            if weekday < 5 and minute >= ao:
                return SessionStatus(
                    "after_hours",
                    self._next_business_day(local),
                    minute - ao,
                    (24 * 60 - minute) + ac,
                )
            # Early-morning ticks already fall on that regular trading date.
            if 1 <= weekday <= 5 and minute < ac:
                return SessionStatus(
                    "after_hours",
                    local.date().isoformat(),
                    (24 * 60 - ao) + minute,
                    ac - minute,
                )

        return SessionStatus(
            "closed",
            local.date().isoformat(),
            0.0,
            0.0,
        )


class RiskStateStore:
    """Atomic persistence for same-trading-day risk accounting.

    Only session risk metrics are restored. Open positions and working orders
    are deliberately not restored from disk; the live gateway must reconcile
    those against TTB and fail closed when the account is not flat.
    """

    VERSION = 1

    def __init__(self, path: Path, symbol: str) -> None:
        self.path = path
        self.symbol = symbol.upper()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self, trading_day: str) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        if value.get("version") != self.VERSION:
            return {}
        if str(value.get("symbol", "")).upper() != self.symbol:
            return {}
        if value.get("trading_day") != trading_day:
            return {}
        return value

    def checkpoint(
        self,
        *,
        trading_day: str,
        realized_pnl: float,
        peak_realized_pnl: float,
        fees_and_tax: float,
        fill_count: int,
        peak_equity: float,
        halt_reason: str,
        position: int,
        strategy_state: dict[str, Any] | None = None,
    ) -> None:
        value = {
            "version": self.VERSION,
            "symbol": self.symbol,
            "trading_day": trading_day,
            "realized_pnl": float(realized_pnl),
            "peak_realized_pnl": float(peak_realized_pnl),
            "fees_and_tax": float(fees_and_tax),
            "fill_count": int(fill_count),
            "peak_equity": float(peak_equity),
            "halt_reason": str(halt_reason or ""),
            "position_at_checkpoint": int(position),
            "strategy_state": strategy_state or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
