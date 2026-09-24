"""Block-bootstrap Monte Carlo for the TTB market-making research strategy.

Use real quote CSV whenever possible.  ``--synthetic-quotes`` exists only to
smoke-test the pipeline; synthetic performance is not evidence of an edge.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from replay_market_maker import MODEL_NOTES, replay
from strategy_core import FillModel, Quote, StrategyConfig


def load_quotes(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) < 3:
        raise ValueError("Monte Carlo requires at least three non-empty quote rows")
    return rows


def synthetic_quotes(
    count: int,
    symbol: str,
    *,
    tick_size: float = 1.0,
    seed: int = 7,
) -> list[dict[str, Any]]:
    """Generate microstructure-like data for tests, not for performance claims."""
    if count < 3:
        raise ValueError("synthetic quote count must be at least three")
    rng = random.Random(seed)
    bid_ticks = 20_000
    direction = 1
    total_volume = 0
    clock = 0.0
    rows: list[dict[str, Any]] = []
    for index in range(count):
        if rng.random() < 0.08:
            direction *= -1
        if rng.random() < 0.24:
            bid_ticks += direction if rng.random() < 0.58 else -direction
        bid_ticks = max(100, bid_ticks)
        bid = bid_ticks * tick_size
        ask = bid + tick_size
        aggressive_buy = rng.random() < 0.5 + 0.08 * direction
        last = ask if aggressive_buy else bid
        last_qty = max(1, int(rng.expovariate(1 / 3)))
        total_volume += last_qty
        depth_bias = direction * rng.uniform(0.0, 0.35)
        base_depth = rng.randint(20, 160)
        bid_qty = max(1, int(base_depth * (1 + depth_bias)))
        ask_qty = max(1, int(base_depth * (1 - depth_bias)))
        clock += max(0.005, rng.expovariate(8.0))
        rows.append(
            asdict(
                Quote(
                    symbol=symbol.upper(),
                    bid=bid,
                    ask=ask,
                    bid_qty=bid_qty,
                    ask_qty=ask_qty,
                    last=last,
                    last_qty=last_qty,
                    total_volume=total_volume,
                    exchange_time=f"SYN-{index:08d}",
                    received_at=clock,
                )
            )
        )
    return rows


def _numeric(row: dict[str, Any], key: str) -> float:
    return float(row[key])


def block_bootstrap(
    rows: list[dict[str, Any]],
    *,
    block_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Circularly resample contiguous blocks and rebase time, volume and price."""
    if block_size < 2:
        raise ValueError("block_size must be at least two")
    if not rows:
        return []
    rng = random.Random(seed)
    n = len(rows)
    positive_deltas = [
        _numeric(rows[i], "received_at") - _numeric(rows[i - 1], "received_at")
        for i in range(1, n)
        if _numeric(rows[i], "received_at") > _numeric(rows[i - 1], "received_at")
    ]
    typical_dt = statistics.median(positive_deltas) if positive_deltas else 0.01
    anchor_last = _numeric(rows[0], "last")
    output: list[dict[str, Any]] = []
    clock = 0.0
    cumulative_volume = 0.0

    while len(output) < n:
        start = rng.randrange(n)
        block = [rows[(start + offset) % n] for offset in range(min(block_size, n - len(output)))]
        price_shift = anchor_last - _numeric(block[0], "last")
        previous_source: dict[str, Any] | None = None
        for source in block:
            if previous_source is None:
                dt = typical_dt
                volume_increment = max(0.0, _numeric(source, "last_qty"))
            else:
                dt = _numeric(source, "received_at") - _numeric(previous_source, "received_at")
                if dt <= 0:
                    dt = typical_dt
                volume_increment = max(
                    0.0,
                    _numeric(source, "total_volume") - _numeric(previous_source, "total_volume"),
                )
            clock += dt
            cumulative_volume += volume_increment
            transformed = dict(source)
            transformed["bid"] = _numeric(source, "bid") + price_shift
            transformed["ask"] = _numeric(source, "ask") + price_shift
            transformed["last"] = _numeric(source, "last") + price_shift
            transformed["bid_qty"] = _numeric(source, "bid_qty")
            transformed["ask_qty"] = _numeric(source, "ask_qty")
            transformed["last_qty"] = int(_numeric(source, "last_qty"))
            transformed["total_volume"] = cumulative_volume
            transformed["received_at"] = clock
            output.append(transformed)
            previous_source = source
        anchor_last = float(output[-1]["last"])
    return output


def percentile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    equity = [result["marked_equity"] for result in results]
    drawdown = [result["max_marked_drawdown"] for result in results]
    cycles = [result["closed_cycles"] for result in results]
    win_rates = [result["cycle_win_rate"] for result in results if result["cycle_win_rate"] is not None]
    profit_factors = [result["profit_factor"] for result in results if result["profit_factor"] is not None]
    returns = [result["cumulative_return"] for result in results if result["cumulative_return"] is not None]
    return {
        "trials": len(results),
        "marked_equity_p05": percentile(equity, 0.05),
        "marked_equity_median": percentile(equity, 0.50),
        "marked_equity_p95": percentile(equity, 0.95),
        "cumulative_return_p05": percentile(returns, 0.05),
        "cumulative_return_median": percentile(returns, 0.50),
        "cumulative_return_p95": percentile(returns, 0.95),
        "loss_probability": sum(value < 0 for value in equity) / len(equity),
        "max_drawdown_median": percentile(drawdown, 0.50),
        "max_drawdown_p95": percentile(drawdown, 0.95),
        "closed_cycles_median": percentile(cycles, 0.50),
        "cycle_win_rate_median": percentile(win_rates, 0.50),
        "profit_factor_median": percentile(profit_factors, 0.50),
    }


def run_monte_carlo(
    rows: list[dict[str, Any]],
    config: StrategyConfig,
    *,
    models: Iterable[FillModel],
    trials: int,
    block_size: int,
    touch_fill_probability: float,
    queue_fraction: float,
    latency_ms: float,
    seed: int,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    if trials <= 0:
        raise ValueError("trials must be positive")
    by_model: dict[str, Any] = {}
    for model in models:
        outcomes = []
        for trial in range(trials):
            path_seed = seed + trial * 104_729
            path = block_bootstrap(rows, block_size=block_size, seed=path_seed)
            outcomes.append(
                replay(
                    path,
                    config,
                    fill_model=model,
                    touch_fill_probability=touch_fill_probability,
                    queue_fraction=queue_fraction,
                    latency_ms=latency_ms,
                    seed=path_seed + 1,
                    initial_capital=initial_capital,
                )
            )
        by_model[model] = {
            "description": MODEL_NOTES[model],
            "summary": summarize(outcomes),
            "outcomes": outcomes,
        }
    independent = by_model.get("independent", {}).get("summary", {}).get("marked_equity_median")
    queue = by_model.get("queue", {}).get("summary", {}).get("marked_equity_median")
    return {
        "models": by_model,
        "independent_minus_queue_median": independent - queue if independent is not None and queue is not None else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", nargs="?", type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--synthetic-quotes", type=int, default=0)
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--block-size", type=int, default=250)
    parser.add_argument("--fill-model", choices=("all", *MODEL_NOTES), default="all")
    parser.add_argument("--touch-fill-probability", type=float, default=0.35)
    parser.add_argument("--queue-fraction", type=float, default=1.0)
    parser.add_argument("--latency-ms", type=float, default=50.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--initial-capital", type=float)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--point-value", type=float, default=200.0)
    parser.add_argument("--fee-per-fill", type=float, default=100.0)
    parser.add_argument("--tax-rate", type=float, default=0.00002)
    parser.add_argument("--target-ticks", type=int, default=8)
    parser.add_argument("--stop-ticks", type=int, default=8)
    parser.add_argument("--half-width-ticks", type=float, default=4.0)
    parser.add_argument("--max-hold-seconds", type=float, default=15.0)
    parser.add_argument("--warmup-ticks", type=int, default=30)
    parser.add_argument("--quote-ttl-seconds", type=float, default=5.0)
    parser.add_argument("--reprice-threshold-ticks", type=float, default=4.0)
    parser.add_argument("--virtual-max", action="store_true")
    parser.add_argument("--sizing-mode", choices=("fixed", "martingale", "anti_martingale"), default="fixed")
    parser.add_argument("--sizing-multiplier", type=float, default=2.0)
    parser.add_argument("--max-sizing-step", type=int, default=4)
    parser.add_argument("--entry-offset-ticks", type=float, default=0.0)
    parser.add_argument("--base-qty", type=int, default=1)
    parser.add_argument("--max-order-qty", type=int, default=1)
    parser.add_argument("--max-inventory", type=int, default=1)
    parser.add_argument("--daily-loss-limit", type=float, default=10_000.0)
    parser.add_argument("--max-drawdown", type=float, default=40_000.0)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.csv_file and args.synthetic_quotes:
        raise SystemExit("choose either csv_file or --synthetic-quotes, not both")
    if not args.csv_file and not args.synthetic_quotes:
        raise SystemExit("provide a quote CSV or --synthetic-quotes N")
    source_is_synthetic = bool(args.synthetic_quotes)
    rows = (
        synthetic_quotes(args.synthetic_quotes, args.symbol, tick_size=args.tick_size, seed=args.seed)
        if source_is_synthetic
        else load_quotes(args.csv_file)
    )
    config = StrategyConfig(
        symbol=args.symbol.upper(),
        tick_size=args.tick_size,
        point_value=args.point_value,
        fee_per_fill=args.fee_per_fill,
        tax_rate=args.tax_rate,
        target_ticks=args.target_ticks,
        stop_ticks=args.stop_ticks,
        half_width_ticks=args.half_width_ticks,
        max_hold_seconds=args.max_hold_seconds,
        warmup_ticks=args.warmup_ticks,
        quote_ttl_seconds=args.quote_ttl_seconds,
        reprice_threshold_ticks=args.reprice_threshold_ticks,
        virtual_max_mode=args.virtual_max,
        sizing_mode=args.sizing_mode,
        sizing_multiplier=args.sizing_multiplier,
        max_sizing_step=args.max_sizing_step,
        entry_offset_ticks=args.entry_offset_ticks,
        base_qty=args.base_qty,
        max_order_qty=args.max_order_qty,
        max_inventory=args.max_inventory,
        recovery_fraction=0,
        daily_loss_limit=args.daily_loss_limit,
        max_drawdown=args.max_drawdown,
    )
    models: tuple[FillModel, ...] = (
        ("independent", "probabilistic", "queue")
        if args.fill_model == "all"
        else (args.fill_model,)
    )
    report = {
        "source": "synthetic-smoke-test" if source_is_synthetic else str(args.csv_file.resolve()),
        "quotes_per_trial": len(rows),
        "block_size": args.block_size,
        "seed": args.seed,
        "config": {
            "symbol": config.symbol,
            "tick_size": config.tick_size,
            "point_value": config.point_value,
            "fee_per_fill": config.fee_per_fill,
            "tax_rate": config.tax_rate,
            "target_ticks": config.target_ticks,
            "stop_ticks": config.stop_ticks,
            "half_width_ticks": config.half_width_ticks,
            "max_hold_seconds": config.max_hold_seconds,
            "quote_ttl_seconds": config.quote_ttl_seconds,
            "reprice_threshold_ticks": config.reprice_threshold_ticks,
            "virtual_max_mode": config.virtual_max_mode,
            "sizing_mode": config.sizing_mode,
            "sizing_multiplier": config.sizing_multiplier,
            "max_sizing_step": config.max_sizing_step,
            "entry_offset_ticks": config.entry_offset_ticks,
            "base_qty": config.base_qty,
            "max_order_qty": config.max_order_qty,
            "max_inventory": config.max_inventory,
            "daily_loss_limit": config.daily_loss_limit,
            "max_drawdown": config.max_drawdown,
            "touch_fill_probability": args.touch_fill_probability,
            "queue_fraction": args.queue_fraction,
            "latency_ms": args.latency_ms,
            "initial_capital": args.initial_capital,
        },
        "warning": (
            "Synthetic data validates code only; every performance number is non-evidentiary."
            if source_is_synthetic
            else "Bootstrap results inherit all omissions and biases in the supplied quote tape."
        ),
    }
    report.update(
        run_monte_carlo(
            rows,
            config,
            models=models,
            trials=args.trials,
            block_size=args.block_size,
            touch_fill_probability=args.touch_fill_probability,
            queue_fraction=args.queue_fraction,
            latency_ms=args.latency_ms,
            seed=args.seed,
            initial_capital=args.initial_capital,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
