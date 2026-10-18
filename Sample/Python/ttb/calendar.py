from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


class CalendarCoverageError(RuntimeError):
    """The exchange schedule is not verified for this date/year."""


@dataclass(frozen=True)
class TradingCalendar:
    starts: date
    ends: date
    closed_dates: frozenset[date]
    source: str

    @classmethod
    def from_file(cls, path: Path) -> "TradingCalendar":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = cls(
                starts=date.fromisoformat(data["starts"]),
                ends=date.fromisoformat(data["ends"]),
                closed_dates=frozenset(date.fromisoformat(text) for text in data["closed_dates"]),
                source=str(data["source"]),
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise CalendarCoverageError("verified TAIFEX calendar missing or invalid") from exc
        if not result.source.strip() or result.starts >= result.ends:
            raise CalendarCoverageError("TAIFEX calendar coverage/source invalid")
        if any(not result.starts <= day <= result.ends for day in result.closed_dates):
            raise CalendarCoverageError("closed date outside verified coverage")
        return result

    def is_trading_day(self, day: date) -> bool:
        if not self.starts <= day <= self.ends:
            raise CalendarCoverageError(f"TAIFEX calendar has no verified coverage for {day}")
        return day.weekday() < 5 and day not in self.closed_dates

    def next_trading_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while cursor <= self.ends:
            if self.is_trading_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise CalendarCoverageError("cannot determine next TAIFEX trading day from verified calendar")


@dataclass(frozen=True)
class ContractInfo:
    symbol: str
    product: str
    month: int
    year: int

    @classmethod
    def from_symbol(cls, symbol: str, reference_year: int) -> "ContractInfo":
        code = symbol.upper().strip()
        match = re.fullmatch(r"(TXF|MTX|TMF)([A-L])(\d)", code)
        if not match:
            raise ValueError(f"unsupported/ambiguous TAIFEX contract code: {symbol}")
        candidates = [year for year in range(reference_year - 5, reference_year + 6)
                      if year % 10 == int(match.group(3))]
        if len(candidates) != 1:
            raise ValueError(f"contract year digit cannot be resolved: {symbol}")
        return cls(code, match.group(1), ord(match.group(2)) - ord("A") + 1, candidates[0])

    @property
    def third_wednesday(self) -> date:
        first = date(self.year, self.month, 1)
        first_wed_offset = (2 - first.weekday()) % 7
        return first + timedelta(days=first_wed_offset + 14)

    def last_trading_day(self, calendar: TradingCalendar) -> date:
        candidate = self.third_wednesday
        if calendar.is_trading_day(candidate):
            return candidate
        # Published product rules allow last day to move to the next business
        # day if the third Wednesday is a non-trading day.
        return calendar.next_trading_day(candidate)

    @classmethod
    def for_month(cls, product: str, year: int, month: int) -> "ContractInfo":
        code = product.upper().strip()
        if code not in SUPPORTED_PRODUCTS:
            raise ValueError(f"unsupported TAIFEX product for automatic rollover: {product}")
        if not 1 <= month <= 12:
            raise ValueError("month must be 1-12")
        return cls(f"{code}{chr(ord('A') + month - 1)}{year % 10}", code, month, year)


SUPPORTED_PRODUCTS = ("TXF", "MTX", "TMF")
AUTO_SYMBOL_PREFIX = "auto:"


def front_month_contract(product: str, today: date, calendar: TradingCalendar) -> ContractInfo:
    """Return the nearest monthly contract that is still the active one.

    The expiring month is abandoned *on* its last trading day: it closes at
    13:30 with no night session, so new exposure belongs in the next month.
    Unverified calendar coverage raises instead of guessing an expiry date.
    """
    contract = ContractInfo.for_month(product, today.year, today.month)
    for _ in range(3):
        if today < contract.last_trading_day(calendar):
            return contract
        year, month = (contract.year + 1, 1) if contract.month == 12 else (contract.year, contract.month + 1)
        contract = ContractInfo.for_month(product, year, month)
    raise CalendarCoverageError("cannot resolve an active front-month contract")


def resolve_symbol(spec: str, today: date, calendar: TradingCalendar | None) -> str:
    """Resolve ``auto:TMF`` style config symbols; explicit codes pass through."""
    text = spec.strip()
    if not text.lower().startswith(AUTO_SYMBOL_PREFIX):
        return text.upper()
    if calendar is None:
        raise CalendarCoverageError("automatic contract rollover requires a verified calendar")
    return front_month_contract(text[len(AUTO_SYMBOL_PREFIX):], today, calendar).symbol

