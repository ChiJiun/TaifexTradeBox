from __future__ import annotations

import argparse
import csv
import json
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from strategy_core import Features, PositionLedger, Quote, StrategyConfig, StrategyDecision
from ttb.calendar import ContractInfo, TradingCalendar
from ttb.engine import BotEngine
from ttb.orders import ShadowGateway
from ttb.runtime import SessionSchedule


TZ = timezone(timedelta(hours=8))
START = datetime(2026, 10, 13, 8, 0, tzinfo=TZ)


@dataclass
class VirtualClock:
    current: datetime = START


class NoTradeSoakStrategy:
    def decide(self, quote: Quote) -> StrategyDecision:
        return StrategyDecision(
            (), Features(0.0, quote.mid, 0.0, 0.0, 0.0),
            False, "soak-no-trading-intent",
        )


def accelerated_soak(
    hours: int = 48,
    interval_minutes: int = 10,
    output: Path | None = None,
) -> dict[str, object]:
    """Replay calendar time with an offline ShadowGateway (zero exchange I/O)."""
    if not 24 <= hours <= 168 or not 1 <= interval_minutes <= 60:
        raise ValueError("soak duration must be 24-168 h and interval 1-60 minutes")
    if hours * 60 % interval_minutes:
        raise ValueError("duration must be divisible by interval")
    owns_dir = tempfile.TemporaryDirectory() if output is None else None
    root = Path(owns_dir.name if owns_dir else output)
    root.mkdir(parents=True, exist_ok=True)
    clock = VirtualClock()
    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current.astimezone(tz) if tz else clock.current.replace(tzinfo=None)

    calendar = TradingCalendar.from_file(
        Path(__file__).resolve().parent / "taifex_calendar_2026.json"
    )
    schedule = SessionSchedule(
        calendar=calendar,
        contract=ContractInfo.from_symbol("TMFK6", 2026),
    )
    config = StrategyConfig(
        symbol="TMFK6", point_value=10,
        min_quote_interval_ms=0, max_order_qty=1, max_inventory=1,
    )
    ledger = PositionLedger(config)
    gateway = ShadowGateway(ledger)
    phases: dict[str, int] = {}
    started = time.monotonic()
    tracemalloc.start()
    try:
        with patch("ttb.engine.datetime", ClockDateTime):
            engine = BotEngine(
                config, gateway, root / "runs",
                strategy=NoTradeSoakStrategy(),
                risk_state_path=root / "risk-state.json",
                health_path=root / "health.json",
                session_schedule=schedule,
            )
            try:
                count = hours * 60 // interval_minutes
                for i in range(count):
                    clock.current = START + timedelta(minutes=interval_minutes * i)
                    phase = schedule.status(clock.current).phase
                    phases[phase] = phases.get(phase, 0) + 1
                    stamp = clock.current.strftime("%H:%M:%S")
                    q = Quote(
                        symbol="TMFK6",
                        bid=20000.0, ask=20001.0,
                        bid_qty=10, ask_qty=9,
                        last=20000.0,
                        last_qty=1,
                        total_volume=i + 1,
                        exchange_time=stamp,
                        received_at=time.monotonic(),
                    )
                    engine.process_quote(q)
                engine.close()
            finally:
                engine.recorder.close()
                engine.raw_recorder.close()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        csv_files = list((root / "runs").glob("*.csv"))
        quote_files = [p for p in csv_files if p.name.startswith("quotes-")]
        decision_files = [p for p in csv_files if p.name.startswith("ttb-shadow-")]
        def count_rows(files):
            total = 0
            for path in files:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    total += sum(1 for _ in csv.DictReader(handle))
            return total
        report = {
            "horizon_hours": hours,
            "interval_minutes": interval_minutes,
            "ticks_processed": count,
            "session_phases": phases,
            "quote_partitions": len(quote_files),
            "decision_partitions": len(decision_files),
            "quotes_saved": count_rows(quote_files),
            "decisions_saved": count_rows(decision_files),
            "broker_orders_submitted": 0,
            "position": ledger.position,
            "peak_python_traced_bytes": peak_bytes,
            "retained_python_traced_bytes": current_bytes,
            "total_output_bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
            "elapsed_wall_seconds": round(time.monotonic() - started, 3),
        }
        if (
            report["quotes_saved"] != count
            or report["decisions_saved"] != count
            or report["quote_partitions"] < 2
            or report["decision_partitions"] < 2
            or report["peak_python_traced_bytes"] >= 50 * 1024 * 1024
            or report["total_output_bytes"] >= 5 * 1024 * 1024
            or report["position"] != 0
        ):
            raise RuntimeError(f"offline soak acceptance failed: {report}")
        return report
    finally:
        tracemalloc.stop()
        if owns_dir:
            owns_dir.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline accelerated 24h+ TTB shadow reliability soak; no broker APIs")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--interval-minutes", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(accelerated_soak(args.hours, args.interval_minutes), indent=2))


if __name__ == "__main__":
    main()
