from __future__ import annotations

import argparse
import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import time
from pathlib import Path
from typing import Any

from strategies import MovingAverageStrategy
from strategy_core import PositionLedger, StrategyConfig
from ttb.api import TTBApi
from ttb.engine import BotEngine, CsvRecorder
from ttb.market_data import QuoteMonitor, ZmqQuoteSubscriber
from ttb.runtime import SessionSchedule, TAIPEI_TZ
from ttb.calendar import (
    AUTO_SYMBOL_PREFIX,
    TradingCalendar,
    ContractInfo,
    CalendarCoverageError,
    resolve_symbol,
)
from datetime import datetime
from ttb.status_server import StatusServer
from ttb.risk import PreTradeLimits
from ttb.contracts import point_value_for
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
    parser.add_argument(
        "--symbol", required=True,
        help="TTB contract code, e.g. TXFA7, or auto:TXF/auto:MTX/auto:TMF for the calendar front month",
    )
    parser.add_argument("--roll-check-seconds", type=float, default=60.0,
                        help="with --symbol auto:PRODUCT, how often to check whether the front month changed")
    parser.add_argument("--mode", choices=("shadow", "ttb"), default="shadow")
    parser.add_argument("--strategy", choices=("market-maker", "moving-average"), default="market-maker")
    parser.add_argument(
        "--strategy-module",
        default="",
        help="optional custom strategy as module:Class; class must accept (config, ledger) and implement decide(quote)",
    )
    parser.add_argument("--strategy-params-json", default="{}",
                        help="JSON object passed as explicit keyword arguments to --strategy-module")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--point-value", type=float, default=None,
                        help="NTD per index point; TXF=200, MTX=50, TMF=10 are auto-resolved")
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
    parser.add_argument("--max-orders-per-minute", type=int, default=20)
    parser.add_argument("--max-cancels-per-minute", type=int, default=60)
    parser.add_argument("--max-daily-orders", type=int, default=250)
    parser.add_argument("--max-notional", type=float, default=0.0,
                        help="mandatory positive notional ceiling for TTB live entries")
    parser.add_argument("--available-margin-field", default="",
                        help="TTB QUERYMARGIN field verified from a real virtual-account fixture")
    parser.add_argument("--min-available-margin", type=float, default=0.0,
                        help="mandatory positive margin headroom for TTB live entries")
    parser.add_argument("--margin-cache-seconds", type=float, default=5.0,
                        help="reuse a QUERYMARGIN read this long while neither the position nor exposure (working orders included) has grown; 0 reads every entry, max 30")
    parser.add_argument("--available-margin-currency", default="TWD",
                        help="QUERYMARGIN row (CURRENCY field) the margin is read from")
    parser.add_argument("--rate-counters-file", type=Path, default=None,
                        help="durable attempt counter file; defaults to output/rate-counters-SYMBOL.json")
    parser.add_argument("--journal-dir", type=Path, default=None,
                        help="append-only execution journal partitioned by trading day, defaults to output/journal")
    parser.add_argument("--short-window", type=int, default=5, help="moving-average strategy")
    parser.add_argument("--long-window", type=int, default=20, help="moving-average strategy")
    parser.add_argument("--entry-ticks", type=float, default=0.5, help="moving-average strategy")
    parser.add_argument("--output", type=Path, default=Path("output") / "runs")
    parser.add_argument("--health-file", type=Path, default=Path("output") / "health.json")
    parser.add_argument("--quote-stale-seconds", type=float, default=5.0)
    parser.add_argument("--max-exchange-lag-seconds", type=float, default=15.0,
                        help="maximum allowed broker TickTime delay in live ZMQ stream")
    parser.add_argument(
        "--stale-fatal-seconds",
        type=float,
        default=120.0,
        help="after quotes have started, exit fail-closed if market data stays stale this long; 0 disables",
    )
    parser.add_argument("--resume-fresh-ticks", type=int, default=3)
    parser.add_argument(
        "--feed-dead-seconds",
        type=float,
        default=0.0,
        help="stop when no symbol on TTB's quote feed ticks this long during a session; "
             "needs a liquid canary subscribed in TTB (supervisor quoteCanary); 0 disables",
    )
    parser.add_argument("--regular-open", default="08:45")
    parser.add_argument("--regular-close", default="13:45")
    parser.add_argument("--after-hours-open", default="15:00")
    parser.add_argument("--after-hours-close", default="05:00")
    parser.add_argument("--disable-after-hours", action="store_true")
    parser.add_argument("--calendar-file", type=Path,
                        default=Path("automation/taifex_calendar_2026.json"),
                        help="TAIFEX verified trading-day calendar; update each year before live trading")
    parser.add_argument("--risk-state-file", type=Path, default=Path("output") / "risk-state.json")
    parser.add_argument("--stop-file", type=Path, default=None,
                        help="operator stop request file; supervisor passes output/supervisor/stop.requested")
    parser.add_argument("--status-host", default="127.0.0.1")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    parser.add_argument("--status-port", type=int, default=8765, help="0 disables the local HTTP status endpoint")
    parser.add_argument(
        "--flatten-on-shutdown",
        action="store_true",
        help="with --mode ttb, attempt an IOC flatten during a graceful stop while the market is open",
    )
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


def configure_logging(args: argparse.Namespace) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, args.log_level))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING if args.log_file else getattr(logging, args.log_level))
    console.setFormatter(formatter)
    root.addHandler(console)
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            args.log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        rotating.setLevel(getattr(logging, args.log_level))
        rotating.setFormatter(formatter)
        root.addHandler(rotating)


def build_config(args: argparse.Namespace) -> StrategyConfig:
    config = StrategyConfig(
        symbol=args.symbol.upper(),
        tick_size=args.tick_size,
        point_value=point_value_for(args.symbol, args.point_value),
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


def _load_strategy_class(spec: str) -> type[Any]:
    module_name, separator, class_name = spec.partition(":")
    if not separator or not module_name.strip() or not class_name.strip():
        raise ValueError("--strategy-module must use module:Class syntax")
    module = importlib.import_module(module_name.strip())
    strategy_class = getattr(module, class_name.strip(), None)
    if strategy_class is None or not callable(strategy_class):
        raise ValueError(f"strategy class not found or not callable: {spec}")
    return strategy_class


def build_strategy(args: argparse.Namespace, config: StrategyConfig, ledger: PositionLedger) -> Any:
    if args.strategy_module:
        strategy_class = _load_strategy_class(args.strategy_module)
        try:
            params = json.loads(args.strategy_params_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--strategy-params-json must contain valid JSON") from exc
        if not isinstance(params, dict) or len(params) > 20 or len(args.strategy_params_json) > 4096:
            raise ValueError("--strategy-params-json must be a small JSON object")
        if any(not isinstance(key, str) or key.startswith("_") for key in params):
            raise ValueError("invalid strategy parameter name")
        strategy = strategy_class(config, ledger, **params)
        if not callable(getattr(strategy, "decide", None)):
            raise TypeError(f"custom strategy {args.strategy_module} must implement decide(quote)")
        return strategy
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


def _resolve_front_month(spec: str, calendar_file: Path) -> str:
    return resolve_symbol(spec, datetime.now(TAIPEI_TZ).date(), TradingCalendar.from_file(calendar_file))


def roll_due(spec: str, current: str, calendar_file: Path, ledger: PositionLedger, gateway: Any) -> bool:
    """True when the front month changed and the bot holds nothing to carry.

    A position or working order is never rolled automatically; the bot keeps
    managing (and exiting) the old contract until it is flat.
    """
    try:
        wanted = _resolve_front_month(spec, calendar_file)
    except (CalendarCoverageError, ValueError) as exc:
        LOG.error("front-month check failed; keeping %s: %s", current, exc)
        return False
    if wanted == current.upper():
        return False
    busy = bool(ledger.position) or bool(getattr(gateway, "active", {})) or bool(getattr(gateway, "pending", {}))
    if busy:
        LOG.warning("front month is %s but %s still has exposure; rollover deferred", wanted, current)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args)
    if args.mode == "ttb" and not args.confirm_virtual_orders:
        raise SystemExit("--mode ttb requires --confirm-virtual-orders")
    if args.mode == "ttb" and not args.confirm_flat_account:
        raise SystemExit(
            "TTB recovery is not automatic: verify a flat account and no orders, then pass --confirm-flat-account"
        )

    if args.max_exchange_lag_seconds <= 0:
        raise SystemExit("--max-exchange-lag-seconds must be positive")
    symbol_spec = args.symbol.strip()
    auto_roll = symbol_spec.lower().startswith(AUTO_SYMBOL_PREFIX)
    if auto_roll:
        try:
            args.symbol = _resolve_front_month(symbol_spec, args.calendar_file)
        except (CalendarCoverageError, ValueError) as exc:
            raise SystemExit(f"cannot resolve {symbol_spec}: {exc}") from exc
        LOG.info("resolved %s to front month %s", symbol_spec, args.symbol)
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
    limits = PreTradeLimits(
        max_orders_per_minute=args.max_orders_per_minute,
        max_cancels_per_minute=args.max_cancels_per_minute,
        max_daily_orders=args.max_daily_orders,
        max_notional=args.max_notional,
        minimum_available_margin=args.min_available_margin,
        available_margin_field=args.available_margin_field,
        available_margin_currency=args.available_margin_currency,
    )
    limits.validate()
    gateway = (
        ShadowGateway(ledger)
        if args.mode == "shadow"
        else TTBOrderGateway(
            api, config, ledger, args.poll_seconds,
            risk_limits=limits, risk_counters_path=args.rate_counters_file,
            journal_dir=args.journal_dir, margin_cache_seconds=args.margin_cache_seconds,
        )
    )
    strategy = build_strategy(args, config, ledger)
    calendar = None
    contract = None
    try:
        if args.calendar_file.exists():
            calendar = TradingCalendar.from_file(args.calendar_file)
        elif args.mode == "ttb":
            raise CalendarCoverageError("TTB live requires a verified --calendar-file")
        if calendar is not None:
            contract = ContractInfo.from_symbol(config.symbol, datetime.now(TAIPEI_TZ).year)
    except (CalendarCoverageError, ValueError) as exc:
        if args.mode == "ttb":
            raise SystemExit(f"TTB live calendar/contract validation failed: {exc}") from exc
        LOG.warning("shadow calendar unavailable or contract unsupported: %s", exc)
        calendar = None
        contract = None
    schedule = SessionSchedule(
        regular_open=args.regular_open,
        regular_close=args.regular_close,
        after_hours_open=args.after_hours_open,
        after_hours_close=args.after_hours_close,
        include_after_hours=not args.disable_after_hours,
        calendar=calendar,
        contract=contract,
    )
    engine = BotEngine(
        config,
        gateway,
        args.output,
        strategy=strategy,
        health_path=args.health_file,
        quote_stale_seconds=args.quote_stale_seconds,
        stale_fatal_seconds=args.stale_fatal_seconds,
        resume_fresh_ticks=args.resume_fresh_ticks,
        session_schedule=schedule,
        risk_state_path=args.risk_state_file,
        flatten_on_shutdown=(args.mode == "ttb" and args.flatten_on_shutdown),
        feed_dead_seconds=args.feed_dead_seconds,
    )
    strategy_name = args.strategy_module or args.strategy
    engine.health.update(mode=args.mode, strategy=strategy_name)
    monitor = QuoteMonitor(
        api, args.zmq_port, config.symbol, engine.submit,
        max_exchange_lag_seconds=args.max_exchange_lag_seconds,
        on_status=lambda stats: engine.health.update(market_data=stats),
    )
    feed = (
        ZmqQuoteSubscriber(args.zmq_port, "", lambda _raw: engine.note_feed_tick())
        if args.feed_dead_seconds > 0 else None
    )
    status_server = StatusServer(args.status_host, args.status_port, engine.health.snapshot)

    def stop_handler(_signum: int, _frame: Any) -> None:
        engine.stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    try:
        engine.start()
        status_server.start()
        monitor.start()
        if feed is not None:
            feed.start()
        LOG.info("monitoring %s; press Ctrl+C to stop", config.symbol)
        next_roll_check = time.monotonic() + max(5.0, args.roll_check_seconds)
        while not engine.stop_event.wait(1.0):
            if args.stop_file is not None and args.stop_file.exists():
                LOG.warning("operator stop requested by %s", args.stop_file)
                engine.stop_event.set()
            if auto_roll and time.monotonic() >= next_roll_check:
                next_roll_check = time.monotonic() + max(5.0, args.roll_check_seconds)
                if roll_due(symbol_spec, config.symbol, args.calendar_file, ledger, gateway):
                    # A clean exit (code 0) lets the supervisor relaunch on
                    # the new contract; shutdown still verifies a flat account.
                    LOG.warning("front month moved away from %s; stopping for rollover", config.symbol)
                    engine.health.update(rollover_from=config.symbol)
                    engine.stop_event.set()
    finally:
        monitor.close()
        if feed is not None:
            feed.close()
        status_server.close()
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
