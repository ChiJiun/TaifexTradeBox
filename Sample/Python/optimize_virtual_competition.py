"""Search TTB-only market-making parameters for terminal net profit.

The optimizer intentionally uses the independent-fill model and the aggressive
``virtual_max_mode``.  It also reports daily Sharpe and known 2026 competition
eligibility fields so a high-profit run that cannot rank is easy to reject.
"""

from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

from monte_carlo_market_maker import synthetic_quotes
from replay_market_maker import replay
from ttb.contracts import point_value_for
from strategy_core import StrategyConfig


def _numbers(text: str, cast: type = float) -> list[Any]:
    values = [part.strip() for part in text.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("parameter list cannot be empty")
    return [cast(value) for value in values]


def _words(text: str) -> list[str]:
    values = [part.strip() for part in text.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("parameter list cannot be empty")
    return values


def resolve_inputs(patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        matches: list[Path]
        if any(character in pattern for character in "*?[]"):
            matches = [Path(item) for item in glob.glob(pattern)]
        elif candidate.is_dir():
            matches = sorted(candidate.glob("quotes-*.csv"))
        else:
            matches = [candidate]
        for match in matches:
            if match.is_file() and match.stat().st_size > 0 and match not in paths:
                paths.append(match)
    return sorted(paths)


def load_days(paths: Iterable[Path]) -> list[list[dict[str, Any]]]:
    days = []
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) >= 3:
            days.append(rows)
    if not days:
        raise ValueError("no input contains at least three quote rows")
    return days


def daily_sharpe(daily_returns: list[float]) -> float | None:
    if len(daily_returns) < 2:
        return None
    deviation = statistics.stdev(daily_returns)
    if deviation == 0:
        return None
    return statistics.mean(daily_returns) / deviation


def maximum_drawdown(values: Iterable[float]) -> float:
    peak = 0.0
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = max(drawdown, peak - value)
    return drawdown


def evaluate(
    days: list[list[dict[str, Any]]],
    config: StrategyConfig,
    *,
    initial_capital: float,
    required_days: int,
    product_count: int,
    seed: int,
) -> dict[str, Any]:
    results = [
        replay(
            rows,
            config,
            fill_model="independent",
            latency_ms=0,
            seed=seed + index,
            initial_capital=initial_capital,
        )
        for index, rows in enumerate(days)
    ]
    daily_pnl = [result["marked_equity"] for result in results]
    daily_returns = [value / initial_capital for value in daily_pnl]
    cumulative = []
    running = 0.0
    for value in daily_pnl:
        running += value
        cumulative.append(running)
    total_pnl = sum(daily_pnl)
    active_days = sum(result["filled_contracts"] > 0 for result in results)
    sharpe = daily_sharpe(daily_returns)
    known_rules_met = (
        total_pnl > 0
        and active_days >= required_days
        and product_count >= 6
    )
    return {
        "parameters": {
            "target_ticks": config.target_ticks,
            "stop_ticks": config.stop_ticks,
            "max_hold_seconds": config.max_hold_seconds,
            "signal_threshold": config.signal_threshold,
            "quote_ttl_seconds": config.quote_ttl_seconds,
            "reprice_threshold_ticks": config.reprice_threshold_ticks,
            "entry_offset_ticks": config.entry_offset_ticks,
            "sizing_mode": config.sizing_mode,
            "sizing_multiplier": config.sizing_multiplier,
            "base_qty": config.base_qty,
            "max_order_qty": config.max_order_qty,
        },
        "total_net_pnl": total_pnl,
        "cumulative_return": total_pnl / initial_capital,
        "daily_sharpe_unannualized": sharpe,
        "active_days": active_days,
        "input_days": len(days),
        "product_count": product_count,
        "known_rules_met": known_rules_met,
        "filled_contracts": sum(result["filled_contracts"] for result in results),
        "closed_cycles": sum(result["closed_cycles"] for result in results),
        "fees_tax": sum(result["fees_tax"] + result["estimated_exit_cost"] for result in results),
        "best_day": max(daily_pnl),
        "worst_day": min(daily_pnl),
        "end_of_day_max_drawdown": maximum_drawdown(cumulative),
        "worst_intraday_drawdown": max(result["max_marked_drawdown"] for result in results),
    }


def _sort_key(result: dict[str, Any], objective: str) -> tuple[float, ...]:
    sharpe = result["daily_sharpe_unannualized"]
    sharpe_value = sharpe if sharpe is not None and math.isfinite(sharpe) else float("-inf")
    if objective == "competition":
        return (
            float(result["known_rules_met"]),
            float(result["total_net_pnl"] > 0),
            sharpe_value,
            result["total_net_pnl"],
        )
    if objective == "sharpe":
        return (sharpe_value, result["total_net_pnl"])
    return (result["total_net_pnl"], sharpe_value)


def search(
    days: list[list[dict[str, Any]]],
    base: StrategyConfig,
    *,
    targets: list[int],
    stops: list[int],
    holds: list[float],
    thresholds: list[float],
    ttls: list[float],
    reprices: list[float],
    offsets: list[float],
    sizing_modes: list[str],
    multipliers: list[float],
    base_quantities: list[int],
    max_quantities: list[int],
    initial_capital: float,
    required_days: int,
    product_count: int,
    seed: int,
    objective: str,
    top: int,
    max_combinations: int,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    grid = itertools.product(
        targets,
        stops,
        holds,
        thresholds,
        ttls,
        reprices,
        offsets,
        sizing_modes,
        multipliers,
        base_quantities,
        max_quantities,
    )
    for values in grid:
        target, stop, hold, threshold, ttl, reprice, offset, sizing, multiplier, base_qty, max_qty = values
        if base_qty > max_qty:
            continue
        effective_multiplier = 1.0 if sizing == "fixed" else multiplier
        identity = (
            target,
            stop,
            hold,
            threshold,
            ttl,
            reprice,
            offset,
            sizing,
            effective_multiplier,
            base_qty,
            max_qty,
        )
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > max_combinations:
            raise ValueError(f"parameter grid exceeds --max-combinations={max_combinations}")
        config = StrategyConfig(
            symbol=base.symbol,
            tick_size=base.tick_size,
            point_value=base.point_value,
            fee_per_fill=base.fee_per_fill,
            tax_rate=base.tax_rate,
            slippage_ticks=base.slippage_ticks,
            target_ticks=target,
            stop_ticks=stop,
            max_hold_seconds=hold,
            signal_threshold=threshold,
            base_qty=base_qty,
            max_order_qty=max_qty,
            max_inventory=max_qty,
            daily_loss_limit=base.daily_loss_limit,
            max_drawdown=base.max_drawdown,
            half_width_ticks=base.half_width_ticks,
            warmup_ticks=base.warmup_ticks,
            quote_ttl_seconds=ttl,
            reprice_threshold_ticks=reprice,
            virtual_max_mode=True,
            sizing_mode=sizing,
            sizing_multiplier=effective_multiplier,
            max_sizing_step=base.max_sizing_step,
            entry_offset_ticks=offset,
            recovery_fraction=0,
        )
        config.validate()
        candidates.append(
            evaluate(
                days,
                config,
                initial_capital=initial_capital,
                required_days=required_days,
                product_count=product_count,
                seed=seed,
            )
        )
    candidates.sort(key=lambda result: _sort_key(result, objective), reverse=True)
    return {
        "objective": objective,
        "evaluated_combinations": len(candidates),
        "initial_capital": initial_capital,
        "known_2026_rules": {
            "positive_total_pnl_required": True,
            "required_trading_days": required_days,
            "required_product_categories": 6,
            "selection": "Sharpe top 30, then total profit top 15",
        },
        "top_results": candidates[:top],
        "note": "Historical maximum is not a guarantee. Re-run on prior days before each future session.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_files", nargs="*")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--synthetic-days", type=int, default=0)
    parser.add_argument("--quotes-per-day", type=int, default=1000)
    parser.add_argument("--objective", choices=("profit", "competition", "sharpe"), default="profit")
    parser.add_argument("--initial-capital", type=float, default=2_000_000)
    parser.add_argument("--product-count", type=int, default=1)
    parser.add_argument("--required-days", type=int, default=28)
    parser.add_argument("--target-ticks", default="3,4,6")
    parser.add_argument("--stop-ticks", default="6,8")
    parser.add_argument("--max-hold-seconds", default="15")
    parser.add_argument("--signal-thresholds", default="0.16")
    parser.add_argument("--quote-ttl-seconds", default="1,5")
    parser.add_argument("--reprice-threshold-ticks", default="2,4")
    parser.add_argument("--entry-offset-ticks", default="0,1")
    parser.add_argument("--sizing-modes", default="fixed,martingale,anti_martingale")
    parser.add_argument("--sizing-multipliers", default="2")
    parser.add_argument("--base-quantities", default="1")
    parser.add_argument("--max-quantities", default="1,4,8")
    parser.add_argument("--max-sizing-step", type=int, default=4)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--point-value", type=float, default=None,
                        help="auto-resolve TXF=200, MTX=50, TMF=10 NTD/point; unknown symbols require explicit value")
    parser.add_argument("--fee-per-fill", type=float, default=100.0)
    parser.add_argument("--tax-rate", type=float, default=0.00002)
    parser.add_argument("--slippage-ticks", type=float, default=0.0)
    parser.add_argument("--half-width-ticks", type=float, default=1.0)
    parser.add_argument("--warmup-ticks", type=int, default=30)
    parser.add_argument("--daily-loss-limit", type=float, default=200_000.0)
    parser.add_argument("--max-drawdown", type=float, default=300_000.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-combinations", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.initial_capital <= 0:
        raise SystemExit("--initial-capital must be positive")
    if args.synthetic_days and args.csv_files:
        raise SystemExit("choose real CSV files or --synthetic-days, not both")
    source: list[str]
    if args.synthetic_days:
        days = [
            synthetic_quotes(
                args.quotes_per_day,
                args.symbol,
                tick_size=args.tick_size,
                seed=args.seed + day,
            )
            for day in range(args.synthetic_days)
        ]
        source = ["synthetic-smoke-test"]
    else:
        paths = resolve_inputs(args.csv_files)
        if not paths:
            raise SystemExit("no non-empty quote CSV files found")
        days = load_days(paths)
        source = [str(path.resolve()) for path in paths]

    modes = _words(args.sizing_modes)
    unknown = set(modes) - {"fixed", "martingale", "anti_martingale"}
    if unknown:
        raise SystemExit(f"unknown sizing modes: {sorted(unknown)}")
    base = StrategyConfig(
        symbol=args.symbol.upper(),
        tick_size=args.tick_size,
        point_value=point_value_for(args.symbol, args.point_value),
        fee_per_fill=args.fee_per_fill,
        tax_rate=args.tax_rate,
        slippage_ticks=args.slippage_ticks,
        half_width_ticks=args.half_width_ticks,
        warmup_ticks=args.warmup_ticks,
        daily_loss_limit=args.daily_loss_limit,
        max_drawdown=args.max_drawdown,
        max_sizing_step=args.max_sizing_step,
    )
    report = search(
        days,
        base,
        targets=_numbers(args.target_ticks, int),
        stops=_numbers(args.stop_ticks, int),
        holds=_numbers(args.max_hold_seconds),
        thresholds=_numbers(args.signal_thresholds),
        ttls=_numbers(args.quote_ttl_seconds),
        reprices=_numbers(args.reprice_threshold_ticks),
        offsets=_numbers(args.entry_offset_ticks),
        sizing_modes=modes,
        multipliers=_numbers(args.sizing_multipliers),
        base_quantities=_numbers(args.base_quantities, int),
        max_quantities=_numbers(args.max_quantities, int),
        initial_capital=args.initial_capital,
        required_days=args.required_days,
        product_count=args.product_count,
        seed=args.seed,
        objective=args.objective,
        top=args.top,
        max_combinations=args.max_combinations,
    )
    report["source"] = source
    report["warning"] = (
        "Synthetic optimization validates code only; its winning parameters are meaningless."
        if args.synthetic_days
        else "Optimize only on past days; reserve later days for confirmation before virtual deployment."
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
