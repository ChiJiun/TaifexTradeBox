"""First-live gate: run the one-lot far-limit order/cancel probe once per contract.

Run from Sample/Python (the supervisor does this before every live launch):

    python -m automation.live_gate --symbol auto:TMF

Exit codes: 0 the contract has a verified probe record (or one was just
completed), 3 deferred (market closed, no quote yet), 2 failed, which also
writes a per-day failure marker so the probe is not repeated that day.

The probe sends ONE real limit order to the logged-in virtual account, far
from the market, and cancels it immediately. It runs only when the operator
has set mode=ttb with both confirmation flags, which is the authorization.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ttb.calendar import CalendarCoverageError, TradingCalendar, resolve_symbol
from ttb.runtime import TAIPEI_TZ, SessionSchedule

VERIFIED = 0
DEFERRED = 3
FAILED = 2


def marker_paths(probe_dir: Path, symbol: str, today: str) -> tuple[Path, Path]:
    code = symbol.upper()
    return probe_dir / f"verified-{code}.json", probe_dir / f"failed-{code}-{today}.json"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def gate(
    *,
    symbol_spec: str,
    calendar_file: Path,
    probe_dir: Path,
    run_probe: Callable[[str], int],
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    now = now or datetime.now(TAIPEI_TZ)
    today = now.date().isoformat()
    try:
        calendar = TradingCalendar.from_file(calendar_file)
        symbol = resolve_symbol(symbol_spec, now.date(), calendar)
    except (CalendarCoverageError, ValueError) as exc:
        return FAILED, {"status": "failed", "reason": f"symbol/calendar: {exc}"}
    verified, failed = marker_paths(probe_dir, symbol, today)
    if verified.is_file():
        return VERIFIED, {"status": "verified", "symbol": symbol, "marker": str(verified)}
    if failed.is_file():
        return FAILED, {"status": "failed", "symbol": symbol, "reason": "probe already failed today; manual check required"}
    if not SessionSchedule(calendar=calendar).status(now).trading:
        return DEFERRED, {"status": "deferred", "symbol": symbol, "reason": "market closed"}
    try:
        code = run_probe(symbol)
    except RuntimeError as exc:
        # ttb_order_probe raises RuntimeError when no quote arrives in time.
        if "no" in str(exc).lower() and "quote" in str(exc).lower():
            return DEFERRED, {"status": "deferred", "symbol": symbol, "reason": str(exc)}
        _write(failed, {"symbol": symbol, "date": today, "error": str(exc)})
        return FAILED, {"status": "failed", "symbol": symbol, "reason": str(exc)}
    except Exception as exc:
        _write(failed, {"symbol": symbol, "date": today, "error": f"{type(exc).__name__}: {exc}"})
        return FAILED, {"status": "failed", "symbol": symbol, "reason": f"{type(exc).__name__}: {exc}"}
    if code == 0:
        _write(verified, {"symbol": symbol, "verified_at": now.isoformat(), "probe_exit_code": 0})
        return VERIFIED, {"status": "verified", "symbol": symbol, "marker": str(verified), "probe_exit_code": 0}
    _write(failed, {"symbol": symbol, "date": today, "probe_exit_code": code})
    return FAILED, {"status": "failed", "symbol": symbol, "probe_exit_code": code,
                    "reason": "probe reported a problem; see output/order-probes and OPERATOR_RUNBOOK.md"}


def _real_probe(args: argparse.Namespace) -> Callable[[str], int]:
    def run(symbol: str) -> int:
        from ttb_order_probe import main as probe_main

        return probe_main([
            "--symbol", symbol, "--side", args.side,
            "--distance-ticks", str(args.distance_ticks),
            "--host", args.host, "--zmq-port", str(args.zmq_port),
            "--output", str(args.probe_dir),
            "--confirm-virtual-order-probe", "--confirm-flat-account", "--confirm-bot-stopped",
        ])
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True, help="auto:TMF or an explicit contract code")
    parser.add_argument("--calendar-file", type=Path, default=Path("automation/taifex_calendar_2026.json"))
    parser.add_argument("--probe-dir", type=Path, default=Path("output/order-probes"))
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--distance-ticks", type=int, default=2000)
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    args = parser.parse_args(argv)
    code, report = gate(
        symbol_spec=args.symbol, calendar_file=args.calendar_file,
        probe_dir=args.probe_dir, run_probe=_real_probe(args),
    )
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
