"""Print the active contract code for a config symbol such as ``auto:TMF``.

Run from Sample/Python: python -m automation.resolve_symbol auto:TMF
Explicit contract codes are echoed unchanged (upper-cased).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from ttb.calendar import CalendarCoverageError, TradingCalendar, resolve_symbol
from ttb.runtime import TAIPEI_TZ


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve auto:PRODUCT to the active TAIFEX front-month code")
    parser.add_argument("symbol", help="auto:TXF, auto:MTX, auto:TMF or an explicit contract code")
    parser.add_argument("--calendar-file", type=Path, default=Path("automation/taifex_calendar_2026.json"))
    parser.add_argument("--date", default="", help="Asia/Taipei date override (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else datetime.now(TAIPEI_TZ).date()
    try:
        print(resolve_symbol(args.symbol, today, TradingCalendar.from_file(args.calendar_file)))
    except (CalendarCoverageError, ValueError) as exc:
        print(f"symbol resolution failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
