"""Replay chronological TTB Quote CSV with selectable execution assumptions."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import deque
from dataclasses import fields
from pathlib import Path
from typing import Any, Iterable

from ttb.contracts import point_value_for

from strategy_core import (
    FillModel,
    MicropriceMarketMaker,
    PositionLedger,
    Quote,
    StrategyConfig,
    make_fill_simulator,
)


MODEL_NOTES = {
    "independent": "TTB-style optimistic bound: later unique trade, shared tick volume, no queue.",
    "probabilistic": "Stress model: strict trade-through fills; at-price fill is Bernoulli.",
    "queue": "Conservative Level-1 proxy: displayed quantity ahead is consumed first.",
}


def _quote_from_row(row: dict[str, Any]) -> Quote:
    values = {f.name: row[f.name] for f in fields(Quote)}
    for key in values:
        if key not in ("symbol", "exchange_time"):
            values[key] = float(values[key])
    values["last_qty"] = int(values["last_qty"])
    return Quote(**values)


def replay(
    rows: Iterable[dict[str, Any]],
    config: StrategyConfig,
    *,
    fill_model: FillModel = "independent",
    touch_fill_probability: float = 0.35,
    queue_fraction: float = 1.0,
    latency_ms: float = 0.0,
    seed: int | None = None,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """Run one path; open inventory is marked but never assumed liquidated."""
    if initial_capital is not None and initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    ledger = PositionLedger(config)
    strategy = MicropriceMarketMaker(config, ledger)
    simulator = make_fill_simulator(
        fill_model,
        touch_fill_probability=touch_fill_probability,
        queue_fraction=queue_fraction,
        latency_ms=latency_ms,
        seed=seed,
    )
    previous_time = float("-inf")
    count = 0
    quote = None
    peak = 0.0
    drawdown = 0.0
    duration = 0.0
    inventory_seconds = 0.0
    cycle_start_pnl: float | None = None
    cycle_pnls: list[float] = []
    observed_hold_seconds: list[float] = []
    submitted_order_ids: set[str] = set()
    filled_order_ids: set[str] = set()
    drift_horizons = (1, 2, 5, 10)
    # A deque per horizon is O(quotes + fills); each drift is evaluated on
    # the FIRST future quote at or after the required horizon.
    drift_pending: dict[int, deque[tuple[float, int, float, int]]] = {
        horizon: deque() for horizon in drift_horizons
    }
    drift_values: dict[int, list[float]] = {horizon: [] for horizon in drift_horizons}

    for row in rows:
        quote = _quote_from_row(row)
        if (
            not quote.is_valid()
            or quote.symbol.upper() != config.symbol.upper()
            or quote.received_at < previous_time
        ):
            raise ValueError("Replay requires valid chronological quotes for one symbol")
        if math.isfinite(previous_time):
            elapsed = quote.received_at - previous_time
            duration += elapsed
            inventory_seconds += abs(ledger.position) * elapsed
        previous_time = quote.received_at
        for horizon in drift_horizons:
            pending = drift_pending[horizon]
            while pending and pending[0][0] <= quote.received_at:
                _deadline, signed, fill_mid, qty = pending.popleft()
                drift_values[horizon].append(
                    signed * (quote.mid - fill_mid) * config.point_value * qty
                )

        for fill in simulator.on_quote(quote):
            filled_order_ids.add(fill.order_id)
            signed = 1 if fill.side == "buy" else -1
            for horizon in drift_horizons:
                drift_pending[horizon].append(
                    (quote.received_at + horizon, signed, quote.mid, fill.quantity)
                )
            old_position = ledger.position
            previous_opened_at = ledger.opened_at
            if old_position == 0:
                cycle_start_pnl = ledger.realized_pnl
            ledger.on_fill(fill)
            if old_position != 0 and ledger.position == 0 and cycle_start_pnl is not None:
                cycle_pnls.append(ledger.realized_pnl - cycle_start_pnl)
                if previous_opened_at:
                    observed_hold_seconds.append(max(0.0, quote.received_at - previous_opened_at))
                cycle_start_pnl = None

        decision = strategy.decide(quote)
        simulator.sync(decision.intents, quote.received_at, quote)
        submitted_order_ids.update(order.order_id for order in simulator.orders.values())
        liquidation = quote.bid if ledger.position > 0 else quote.ask
        equity = ledger.mark_to_market(liquidation)
        # Marked-to-market risk includes already incurred execution slippage,
        # even when a position is held or the final fill has just flattened.
        equity -= ledger.fill_count * config.slippage_ticks * config.tick_value
        if ledger.position:
            equity -= abs(ledger.position) * (
                config.fee_per_fill
                + abs(liquidation * config.point_value * config.tax_rate)
                + config.slippage_ticks * config.tick_value
            )
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        count += 1

    gains = sum(value for value in cycle_pnls if value > 0)
    losses = -sum(value for value in cycle_pnls if value < 0)
    wins = sum(value > 0 for value in cycle_pnls)
    raw_marked_equity = (
        ledger.mark_to_market(quote.bid if ledger.position > 0 else quote.ask)
        if quote
        else 0.0
    )
    estimated_exit_cost = 0.0
    if quote and ledger.position:
        liquidation = quote.bid if ledger.position > 0 else quote.ask
        estimated_exit_cost = abs(ledger.position) * (
            config.fee_per_fill
            + abs(liquidation * config.point_value * config.tax_rate)
            + config.slippage_ticks * config.tick_value
        )
    marked_equity = raw_marked_equity - estimated_exit_cost
    slippage_cost = ledger.fill_count * config.slippage_ticks * config.tick_value
    net_marked_equity = marked_equity - slippage_cost
    net_realized = ledger.realized_pnl - slippage_cost
    sorted_holds = sorted(observed_hold_seconds)
    p95_hold = (
        sorted_holds[min(len(sorted_holds) - 1, math.ceil(0.95 * len(sorted_holds)) - 1)]
        if sorted_holds else None
    )
    return {
        "quotes": count,
        "filled_contracts": ledger.fill_count,
        "closed_cycles": len(cycle_pnls),
        "cycle_win_rate": wins / len(cycle_pnls) if cycle_pnls else None,
        "average_cycle_net": sum(cycle_pnls) / len(cycle_pnls) if cycle_pnls else None,
        "profit_factor": gains / losses if losses > 0 else None,
        "realized_net": ledger.realized_pnl,
        "net_realized_after_slippage": net_realized,
        "slippage_cost": slippage_cost,
        "fees_tax": ledger.fees_and_tax,
        "open_position": ledger.position,
        "raw_marked_equity": raw_marked_equity,
        "estimated_exit_cost": estimated_exit_cost,
        "marked_equity": marked_equity,
        "net_marked_equity": net_marked_equity,
        "cumulative_return": marked_equity / initial_capital if initial_capital else None,
        "net_cumulative_return": net_marked_equity / initial_capital if initial_capital else None,
        "submitted_orders": len(submitted_order_ids),
        "filled_orders": len(filled_order_ids),
        "order_fill_rate": len(filled_order_ids) / len(submitted_order_ids) if submitted_order_ids else None,
        "inventory_hold_seconds_p95": p95_hold,
        "inventory_hold_samples": len(observed_hold_seconds),
        "open_inventory_seconds_at_end": max(0.0, quote.received_at - ledger.opened_at) if quote and ledger.position and ledger.opened_at else 0.0,
        "adverse_selection": {
            str(horizon): {
                "samples": len(drift_values[horizon]),
                "mean_signed_mid_drift_ntd": (
                    sum(drift_values[horizon]) / len(drift_values[horizon])
                    if drift_values[horizon] else None
                ),
            }
            for horizon in drift_horizons
        },
        "max_marked_drawdown": drawdown,
        "inventory_unit_seconds": inventory_seconds,
        "inventory_exposure_ratio": inventory_seconds / duration if duration > 0 else 0.0,
        "halt_reason": strategy.halt_reason,
        "fill_model": fill_model,
        "model": MODEL_NOTES[fill_model],
        "assumptions": {
            "touch_fill_probability": touch_fill_probability if fill_model == "probabilistic" else None,
            "queue_fraction": queue_fraction if fill_model == "queue" else None,
            "latency_ms": latency_ms,
        },
        "strategy": {
            "virtual_max_mode": config.virtual_max_mode,
            "sizing_mode": config.sizing_mode,
            "sizing_multiplier": config.sizing_multiplier,
            "max_sizing_step": config.max_sizing_step,
            "entry_offset_ticks": config.entry_offset_ticks,
            "base_qty": config.base_qty,
            "max_order_qty": config.max_order_qty,
            "max_inventory": config.max_inventory,
        },
        "note": "Open inventory is marked, not assumed liquidated. No profitability claim.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--fill-model", choices=tuple(MODEL_NOTES), default="independent")
    parser.add_argument("--touch-fill-probability", type=float, default=0.35)
    parser.add_argument("--queue-fraction", type=float, default=1.0)
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--initial-capital", type=float)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--point-value", type=float, default=None,
                        help="auto-resolve TXF=200, MTX=50, TMF=10 NTD/point; unknown symbols require explicit value")
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
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = StrategyConfig(
        symbol=args.symbol.upper(),
        tick_size=args.tick_size,
        point_value=point_value_for(args.symbol, args.point_value),
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
    with args.csv_file.open(encoding="utf-8-sig", newline="") as stream:
        result = replay(
            csv.DictReader(stream),
            config,
            fill_model=args.fill_model,
            touch_fill_probability=args.touch_fill_probability,
            queue_fraction=args.queue_fraction,
            latency_ms=args.latency_ms,
            seed=args.seed,
            initial_capital=args.initial_capital,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
