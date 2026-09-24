from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from strategies import MovingAverageStrategy
from strategy_core import PositionLedger, StrategyConfig
from ttb.api import TTBApi
from ttb.engine import BotEngine, CsvRecorder
from ttb.market_data import QuoteMonitor, ZmqQuoteSubscriber
from ttb.orders import (
    ShadowGateway,
    TTBOrderGateway,
    _float,
    _recursive_json,
    _response_rows,
    _side,
    _value,
)


# These imports intentionally re-export the old class/helper names. Code that
# imported TTBOrderGateway or _float from this script therefore keeps working.
LOG = logging.getLogger("ttb_sim_hft")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modular TTB quote monitor and strategy runner")
    parser.add_argument("--symbol", required=True, help="TTB contract code, e.g. TXFA7")
    parser.add_argument("--mode", choices=("shadow", "ttb"), default="shadow")
    parser.add_argument("--strategy", choices=("market-maker", "moving-average"), default="market-maker")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--point-value", type=float, default=200.0)
    parser.add_argument("--fee-per-fill", type=float, default=100.0)
    parser.add_argument("--tax-rate", type=float, default=0.00002)
    parser.add_argument("--slippage-ticks", type=float, default=0.0)
    parser.add_argument("--target-ticks", type=int, default=8)
    parser.add_argument("--half-width-ticks", type=float, default=4.0)
    parser.add_argument("--warmup-ticks", type=int, default=30)
    parser.add_argument("--quote-ttl-seconds", type=float, default=5.0)
    parser.add_argument("--reprice-threshold-ticks", type=float, default=4.0)
    parser.add_argument("--virtual-max", action="store_true", help="TTB-only aggressive best-price entry mode")
    parser.add_argument("--sizing-mode", choices=("fixed", "martingale", "anti_martingale"), default="fixed")
    parser.add_argument("--sizing-multiplier", type=float, default=2.0)
    parser.add_argument("--max-sizing-step", type=int, default=4)
    parser.add_argument("--entry-offset-ticks", type=float, default=0.0)
    parser.add_argument("--stop-ticks", type=int, default=8)
    parser.add_argument("--max-hold-seconds", type=float, default=15.0)
    parser.add_argument("--signal-threshold", type=float, default=0.16)
    parser.add_argument("--base-qty", type=int, default=1)
    parser.add_argument("--max-order-qty", type=int, default=1)
    parser.add_argument("--max-inventory", type=int, default=1)
    parser.add_argument("--recovery-fraction", type=float, default=0.0, help="legacy strategy only")
    parser.add_argument("--daily-loss-limit", type=float, default=10_000.0)
    parser.add_argument("--max-drawdown", type=float, default=40_000.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--short-window", type=int, default=5, help="moving-average strategy")
    parser.add_argument("--long-window", type=int, default=20, help="moving-average strategy")
    parser.add_argument("--entry-ticks", type=float, default=0.5, help="moving-average strategy")
    parser.add_argument("--output", type=Path, default=Path("output") / "runs")
    parser.add_argument("--health-file", type=Path, default=Path("output") / "health.json")
    parser.add_argument("--quote-stale-seconds", type=float, default=5.0)
    parser.add_argument("--resume-fresh-ticks", type=int, default=3)
    parser.add_argument(
        "--confirm-virtual-orders",
        action="store_true",
        help="required with --mode ttb; confirms orders may be sent to the logged-in virtual account",
    )
    parser.add_argument(
        "--confirm-flat-account",
        action="store_true",
        help="required with --mode ttb; confirms the account has no position or working order",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> StrategyConfig:
    config = StrategyConfig(
        symbol=args.symbol.upper(),
        tick_size=args.tick_size,
        point_value=args.point_value,
        fee_per_fill=args.fee_per_fill,
        tax_rate=args.tax_rate,
        slippage_ticks=args.slippage_ticks,
        target_ticks=args.target_ticks,
        half_width_ticks=args.half_width_ticks,
        warmup_ticks=args.warmup_ticks,
        quote_ttl_seconds=args.quote_ttl_seconds,
        reprice_threshold_ticks=args.reprice_threshold_ticks,
        virtual_max_mode=args.virtual_max,
        sizing_mode=args.sizing_mode,
        sizing_multiplier=args.sizing_multiplier,
        max_sizing_step=args.max_sizing_step,
        entry_offset_ticks=args.entry_offset_ticks,
        stop_ticks=args.stop_ticks,
        max_hold_seconds=args.max_hold_seconds,
        signal_threshold=args.signal_threshold,
        base_qty=args.base_qty,
        max_order_qty=args.max_order_qty,
        max_inventory=args.max_inventory,
        recovery_fraction=args.recovery_fraction,
        daily_loss_limit=args.daily_loss_limit,
        max_drawdown=args.max_drawdown,
    )
    config.validate()
    return config


def build_strategy(args: argparse.Namespace, config: StrategyConfig, ledger: PositionLedger) -> Any:
    if args.strategy == "moving-average":
        return MovingAverageStrategy(
            config,
            ledger,
            short_window=args.short_window,
            long_window=args.long_window,
            entry_ticks=args.entry_ticks,
        )
    # None asks BotEngine to construct the default market maker.
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.mode == "ttb" and not args.confirm_virtual_orders:
        raise SystemExit("--mode ttb requires --confirm-virtual-orders")
    if args.mode == "ttb" and not args.confirm_flat_account:
        raise SystemExit(
            "TTB recovery is not automatic: verify a flat account and no orders, then pass --confirm-flat-account"
        )

    config = build_config(args)
    LOG.info(
        "symbol=%s mode=%s strategy=%s target=%s ticks estimated_min_ticks=%s cycle_net_at_40000=%.2f",
        config.symbol,
        args.mode,
        args.strategy,
        config.target_ticks,
        config.minimum_profitable_ticks(40_000),
        config.estimated_cycle_net(40_000),
    )

    api = TTBApi.connect(args.host, args.zmq_port)
    ledger = PositionLedger(config)
    gateway = (
        ShadowGateway(ledger)
        if args.mode == "shadow"
        else TTBOrderGateway(api, config, ledger, args.poll_seconds)
    )
    strategy = build_strategy(args, config, ledger)
    engine = BotEngine(
        config,
        gateway,
        args.output,
        strategy=strategy,
        health_path=args.health_file,
        quote_stale_seconds=args.quote_stale_seconds,
        resume_fresh_ticks=args.resume_fresh_ticks,
    )
    engine.health.update(mode=args.mode, strategy=args.strategy)
    monitor = QuoteMonitor(api, args.zmq_port, config.symbol, engine.submit)

    def stop_handler(_signum: int, _frame: Any) -> None:
        engine.stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    engine.start()
    monitor.start()
    LOG.info("monitoring %s; press Ctrl+C to stop", config.symbol)
    try:
        while not engine.stop_event.wait(1.0):
            pass
    finally:
        monitor.close()
        engine.close()
        LOG.info(
            "finished realized=%.2f fees_tax=%.2f position=%s output=%s",
            ledger.realized_pnl,
            ledger.fees_and_tax,
            ledger.position,
            engine.recorder.path,
        )
    return 2 if engine.fatal_error or ledger.position else 0


if __name__ == "__main__":
    sys.exit(main())
