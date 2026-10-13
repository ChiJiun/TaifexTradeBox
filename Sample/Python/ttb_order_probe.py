from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from strategy_core import Quote
from ttb.api import TTBApi
from ttb.market_data import QuoteMonitor
from ttb.probe import OrderProbe, write_probe_result


LOG = logging.getLogger("ttb_order_probe")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TTB virtual-account far-limit order/cancel probe")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--distance-ticks", type=int, default=2_000)
    parser.add_argument("--quote-timeout", type=float, default=15.0)
    parser.add_argument("--ack-timeout", type=float, default=10.0)
    parser.add_argument("--cancel-timeout", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("output") / "order-probes")
    parser.add_argument("--confirm-virtual-order-probe", action="store_true")
    parser.add_argument("--confirm-flat-account", action="store_true")
    parser.add_argument("--confirm-bot-stopped", action="store_true",
                        help="operator verified no bot is actively controlling the GUI account")
    parser.add_argument("--supervisor-dir", type=Path, default=Path("output/supervisor"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not args.confirm_virtual_order_probe:
        raise SystemExit("probe requires --confirm-virtual-order-probe")
    if not args.confirm_flat_account:
        raise SystemExit("verify no position and no working orders, then pass --confirm-flat-account")
    if not args.confirm_bot_stopped:
        raise SystemExit("probe requires --confirm-bot-stopped")
    for name in ("manual-reconciliation-required.flag", "stop.requested"):
        if (args.supervisor_dir / name).exists():
            raise SystemExit(f"probe forbidden while supervisor safety flag exists: {name}")
    api = TTBApi.connect(args.host, args.zmq_port)
    received = threading.Event()
    latest: list[Quote] = []

    def on_quote(quote: Quote) -> None:
        latest[:] = [quote]
        received.set()

    monitor = QuoteMonitor(api, args.zmq_port, args.symbol, on_quote)
    monitor.start()
    try:
        if not received.wait(args.quote_timeout):
            raise RuntimeError(f"no {args.symbol.upper()} quote received within {args.quote_timeout:g}s")
        probe = OrderProbe(
            api,
            args.symbol,
            side=args.side,
            tick_size=args.tick_size,
            distance_ticks=args.distance_ticks,
            ack_timeout=args.ack_timeout,
            cancel_timeout=args.cancel_timeout,
        )
        result = probe.run(latest[0])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = args.output / f"order-probe-{args.symbol.upper()}-{stamp}.json"
        write_probe_result(output, result)
        LOG.info(
            "probe result order=%s accepted=%s cancel_accepted=%s cancel_confirmed=%s fills=%s output=%s",
            result.order_no,
            result.order_accepted,
            result.cancel_accepted,
            result.cancel_confirmed,
            result.unexpected_fill_quantity,
            output,
        )
        if result.unexpected_fill_quantity:
            return 3
        if not result.cancel_confirmed:
            return 2
        return 0 if result.position_query_succeeded else 4
    finally:
        monitor.close()


if __name__ == "__main__":
    sys.exit(main())
