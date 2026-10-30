from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .calendar import TradingCalendar, ContractInfo, CalendarCoverageError
from .health import REPLACE_ATTEMPTS


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
    reason: str = ""

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
    calendar: TradingCalendar | None = None
    contract: ContractInfo | None = None

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

        if self.calendar is not None:
            return self._calendar_status(local, minute, ro, rc, ao, ac)

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
                # Saturday 00:00-05:00 still belongs to Friday's night
                # session and is accounted for on the next business day.
                trading_day = (
                    self._next_business_day(local - timedelta(days=1))
                    if weekday == 5 else local.date().isoformat()
                )
                return SessionStatus(
                    "after_hours",
                    trading_day,
                    (24 * 60 - ao) + minute,
                    ac - minute,
                )

        return SessionStatus(
            "closed",
            local.date().isoformat(),
            0.0,
            0.0,
        )

    def _calendar_status(
        self, local: datetime, minute: float,
        ro: int, rc: int, ao: int, ac: int,
    ) -> SessionStatus:
        assert self.calendar is not None
        day = local.date()
        day_text = day.isoformat()
        closed = lambda reason: SessionStatus("closed", day_text, 0.0, 0.0, reason)
        try:
            expiry = self.contract.last_trading_day(self.calendar) if self.contract else None
            if expiry is not None and day > expiry:
                return closed("manual_contract_rollover_required")

            current_open = self.calendar.is_trading_day(day)
            warn = (
                "contract_expiring_soon" if expiry and 0 < (expiry - day).days <= 7
                else "contract_expiry_today" if expiry == day
                else ""
            )
            regular_close = min(rc, 13 * 60 + 30) if expiry == day else rc
            if current_open and ro <= minute < regular_close:
                return SessionStatus(
                    "regular", day_text,
                    minute - ro, regular_close - minute, warn,
                )

            if self.include_after_hours:
                if current_open and minute >= ao and expiry != day:
                    next_date = self.calendar.next_trading_day(day)
                    return SessionStatus(
                        "after_hours", next_date.isoformat(),
                        minute - ao, 24 * 60 - minute + ac, warn,
                    )
                # A night session before a holiday/weekend keeps running
                # until 05:00 on the following calendar day, using the same
                # NEXT VALID trading-day key as the prior evening.
                previous = day - timedelta(days=1)
                if minute < ac and self.calendar.is_trading_day(previous) and expiry != previous:
                    next_date = self.calendar.next_trading_day(previous)
                    return SessionStatus(
                        "after_hours", next_date.isoformat(),
                        24 * 60 - ao + minute, ac - minute, warn,
                    )
            return closed(
                "contract_expiry_today" if expiry == day else
                "exchange_holiday" if not current_open else "outside_trading_hours"
            )
        except CalendarCoverageError:
            return closed("calendar_unverified")


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
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("persisted risk state unreadable; manual reconciliation required") from exc
        if not isinstance(value, dict) or value.get("version") != self.VERSION:
            raise RuntimeError("persisted risk state format invalid; manual reconciliation required")
        if not isinstance(value.get("trading_day"), str):
            raise RuntimeError("persisted risk state trading day missing; manual reconciliation required")
        # A previous day's file carries no risk budget for today, so a strategy
        # or symbol switch between sessions must not trip the symbol guard.
        if value["trading_day"] != trading_day:
            return {}
        if str(value.get("symbol", "")).upper() != self.symbol:
            raise RuntimeError("persisted risk state symbol mismatched; manual reconciliation required")
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
            # Every decision checkpoints, and a reader that briefly opens the
            # file without FILE_SHARE_DELETE (antivirus, an operator's editor)
            # makes the Windows replace fail; retry before failing the session.
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError:
                    if attempt + 1 == REPLACE_ATTEMPTS:
                        raise
                    time.sleep(0.05 * (attempt + 1))
