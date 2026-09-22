from __future__ import annotations

import argparse
import csv
import json
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strategy_core import (
    Fill,
    IndependentFillSimulator,
    IndependentFillStrategy,
    OrderIntent,
    PositionLedger,
    Quote,
    StrategyConfig,
)


LOG = logging.getLogger("ttb_sim_hft")


def _recursive_json(value: Any) -> Any:
    current = value
    for _ in range(3):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text or text[0] not in "[{\"":
            break
        try:
            current = json.loads(text)
        except json.JSONDecodeError:
            break
    return current


def _response_rows(response: Any) -> list[dict[str, Any]]:
    parsed = _recursive_json(response)
    if isinstance(parsed, dict):
        parsed = _recursive_json(parsed.get("Data", parsed.get("data", [])))
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    return []


def _value(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return default


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").replace("-", "0"))
    except (TypeError, ValueError):
        return 0.0


def _side(value: Any) -> str:
    text = str(value).strip().lower()
    return "buy" if text in {"1", "b", "buy", "買", "買別", "買進"} else "sell"


class CsvRecorder:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"ttb-shadow-{stamp}.csv"
        self._stream = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(
            self._stream,
            fieldnames=[
                "wall_time", "exchange_time", "symbol", "bid", "ask", "last", "last_qty",
                "bid_qty", "ask_qty", "score", "trade_flow", "position", "average_price",
                "realized_pnl", "mark_to_market", "decision", "active_orders",
            ],
        )
        self._writer.writeheader()

    def write(self, quote: Quote, decision: Any, ledger: PositionLedger, orders: Any) -> None:
        self._writer.writerow(
            {
                "wall_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "exchange_time": quote.exchange_time,
                "symbol": quote.symbol,
                "bid": quote.bid,
                "ask": quote.ask,
                "last": quote.last,
                "last_qty": quote.last_qty,
                "bid_qty": quote.bid_qty,
                "ask_qty": quote.ask_qty,
                "score": round(decision.features.score, 6),
                "trade_flow": round(decision.features.trade_flow, 6),
                "position": ledger.position,
                "average_price": ledger.average_price,
                "realized_pnl": round(ledger.realized_pnl, 2),
                "mark_to_market": round(ledger.mark_to_market(quote.last), 2),
                "decision": decision.reason,
                "active_orders": json.dumps(orders, ensure_ascii=False),
            }
        )
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


class ShadowGateway:
    def __init__(self, ledger: PositionLedger) -> None:
        self.ledger = ledger
        self.simulator = IndependentFillSimulator()

    def before_decision(self, quote: Quote) -> list[Fill]:
        fills = self.simulator.on_quote(quote)
        for fill in fills:
            delta = self.ledger.on_fill(fill)
            LOG.info(
                "SIM FILL %s %s @ %.2f id=%s pnl_delta=%.2f position=%s realized=%.2f",
                fill.side,
                fill.quantity,
                fill.price,
                fill.order_id,
                delta,
                self.ledger.position,
                self.ledger.realized_pnl,
            )
        return fills

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None:
        self.simulator.sync(intents, quote.received_at)

    def snapshot(self) -> dict[str, Any]:
        return {
            side: {"price": order.intent.price, "qty": order.remaining, "role": order.intent.role}
            for side, order in self.simulator.orders.items()
        }


class TTBOrderGateway:
    """Fail-closed TTB order gateway with one working order per side."""

    def __init__(self, module: Any, config: StrategyConfig, ledger: PositionLedger, poll_seconds: float) -> None:
        self.module = module
        self.config = config
        self.ledger = ledger
        self.poll_seconds = max(0.25, poll_seconds)
        self.active: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.owned_order_nos: set[str] = set()
        self.preexisting_order_nos: set[str] = set()
        self.seen_deal_qty: dict[str, int] = {}
        self.last_poll = 0.0
        self.bootstrapped = False

    def before_decision(self, quote: Quote) -> list[Fill]:
        if time.monotonic() - self.last_poll < self.poll_seconds:
            return []
        self.last_poll = time.monotonic()
        response = self.module.QUERYRESTOREREPORT()
        rows = _response_rows(response)
        if not self.bootstrapped:
            self.preexisting_order_nos = {
                str(_value(row, "ORDNO", "OrdNo", "OrderNo", default=""))
                for row in rows
                if str(_value(row, "ORDNO", "OrdNo", "OrderNo", default=""))
            }
            self.bootstrapped = True
            LOG.info("TTB order baseline loaded: %s existing order ids ignored", len(self.preexisting_order_nos))
            return []
        fills: list[Fill] = []
        for row in rows:
            symbol = str(_value(row, "COMD_ID", "ComdID1", "Symbol", default="")).split(".")[0].upper()
            if symbol and symbol != self.config.symbol.upper():
                continue
            order_no = str(_value(row, "ORDNO", "OrdNo", "OrderNo", default=""))
            if not order_no:
                continue
            side = _side(_value(row, "BS", "Side", "ComdBS1"))
            price = _float(_value(row, "DEAL_PRCE", "DealPrice", default=0))
            ordered_price = _float(_value(row, "ORDR_PRCE", "Price", default=0))
            ordered_qty = int(_float(_value(row, "VOLM", "OrderQty", default=0)))
            if order_no not in self.owned_order_nos:
                pending = self.pending.get(side)
                intent = pending.get("intent") if pending else None
                matches_pending = (
                    intent is not None
                    and ordered_price == intent.price
                    and (ordered_qty == 0 or ordered_qty == intent.quantity)
                    and order_no not in self.preexisting_order_nos
                )
                if not matches_pending:
                    continue
                self.owned_order_nos.add(order_no)
                self.pending.pop(side, None)
                LOG.info("TTB BIND %s to order %s", side, order_no)
            deal_total = int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0)))
            previous = self.seen_deal_qty.get(order_no, 0)
            if deal_total > previous and price > 0:
                fill = Fill(side, price, deal_total - previous, order_no, quote.received_at)
                self.ledger.on_fill(fill)
                fills.append(fill)
            self.seen_deal_qty[order_no] = max(previous, deal_total)
            remaining = int(_float(_value(row, "LESS_VOLM", "RemainQty", default=0)))
            status = str(_value(row, "STAT", "Status", default=""))
            if remaining > 0 and not any(token in status for token in ("刪", "取消", "完全成交")):
                self.active[side] = {"order_no": order_no, "price": ordered_price, "qty": remaining}
            elif self.active.get(side, {}).get("order_no") == order_no:
                self.active.pop(side, None)
        return fills

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None:
        desired = {intent.side: intent for intent in intents if intent.quantity > 0}
        for side in ("buy", "sell"):
            current = self.active.get(side)
            wanted = desired.get(side)
            if current and wanted is None:
                self._cancel(side, current)
                continue
            if current and wanted and (
                current["price"] != wanted.price or current["qty"] != wanted.quantity
            ):
                self._cancel(side, current)
                current = None
            if wanted and not current and side not in self.pending:
                self._place(wanted)

    def _place(self, intent: OrderIntent) -> None:
        payload = {
            "Symbol1": self.config.symbol,
            "Price": str(intent.price),
            "TimeInForce": "2" if intent.role == "risk_exit" else "1",
            "Side1": "1" if intent.side == "buy" else "2",
            "OrderType": "2",
            "OrderQty": str(intent.quantity),
            "DayTrade": "0",
            "Symbol2": "",
            "Side2": "",
            "PositionEffect": "4",
        }
        response = self.module.NEWORDER(payload)
        code = str(response.get("Code", response.get("code", ""))) if isinstance(response, dict) else ""
        if code not in {"0000", "000"}:
            raise RuntimeError(f"TTB new order rejected: {response}")
        order_no = ""
        if isinstance(response, dict):
            order_no = str(
                response.get("OrdNo")
                or response.get("OrderNo")
                or response.get("ORDNO")
                or ""
            )
        if order_no:
            self.owned_order_nos.add(order_no)
            self.active[intent.side] = {
                "order_no": order_no,
                "price": intent.price,
                "qty": intent.quantity,
            }
        else:
            self.pending[intent.side] = {"intent": intent, "created_at": time.monotonic()}
        LOG.info("TTB NEW %s qty=%s price=%s role=%s response=%s", intent.side, intent.quantity, intent.price, intent.role, response)

    def _cancel(self, side: str, current: dict[str, Any]) -> None:
        response = self.module.CANCELORDER({"OrdNo": current["order_no"]})
        code = str(response.get("Code", response.get("code", ""))) if isinstance(response, dict) else ""
        if code not in {"0000", "000"}:
            raise RuntimeError(f"TTB cancel rejected: {response}")
        LOG.info("TTB CANCEL %s %s", side, current["order_no"])
        self.active.pop(side, None)

    def cancel_all(self) -> None:
        for side, current in list(self.active.items()):
            try:
                self._cancel(side, current)
            except Exception:
                LOG.exception("unable to cancel %s", current)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "pending": {k: asdict(v["intent"]) for k, v in self.pending.items()},
        }


class ZmqQuoteSubscriber:
    """Stoppable version of the quote loop bundled in TTBHelp.pyc."""

    def __init__(self, port: int, symbol: str, callback: Any) -> None:
        import zmq

        self.zmq = zmq
        self.symbol = symbol.upper()
        self.topic = f"TTB$Tick${self.symbol}"
        self.callback = callback
        self.stop_event = threading.Event()
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.RCVHWM, 0)
        self.socket.setsockopt(zmq.RCVTIMEO, 500)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, "TTB$Tick")
        self.socket.connect(f"tcp://localhost:{port}")
        self.thread = threading.Thread(target=self._run, name="ttb-zmq", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                topic = self.socket.recv().decode("utf-8")
                message = self.socket.recv()
            except self.zmq.Again:
                continue
            except Exception:
                if not self.stop_event.is_set():
                    LOG.exception("ZMQ quote receive failed")
                break
            if topic != self.topic:
                continue
            try:
                self.callback(json.loads(message))
            except Exception:
                LOG.exception("invalid quote message")

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.socket.close(linger=0)
        self.context.term()


class BotEngine:
    def __init__(self, config: StrategyConfig, gateway: Any, output: Path) -> None:
        self.config = config
        self.ledger = gateway.ledger
        self.strategy = IndependentFillStrategy(config, self.ledger)
        self.gateway = gateway
        self.recorder = CsvRecorder(output)
        self.queue: queue.Queue[Quote] = queue.Queue(maxsize=20_000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="strategy-engine", daemon=True)
        self.last_decision_at = 0.0

    def start(self) -> None:
        self.thread.start()

    def submit(self, raw: dict[str, Any]) -> None:
        quote = Quote.from_ttb(raw)
        if quote.symbol and quote.symbol != self.config.symbol.upper():
            return
        try:
            self.queue.put_nowait(quote)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(quote)

    def _run(self) -> None:
        min_interval = self.config.min_quote_interval_ms / 1000.0
        while not self.stop_event.is_set():
            try:
                quote = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.gateway.before_decision(quote)
                if quote.received_at - self.last_decision_at < min_interval:
                    continue
                decision = self.strategy.decide(quote)
                self.gateway.sync(decision.intents, quote)
                self.last_decision_at = quote.received_at
                self.recorder.write(quote, decision, self.ledger, self.gateway.snapshot())
                LOG.info(
                    "%s last=%.2f score=%+.3f flow=%+.3f pos=%s pnl=%.2f orders=%s",
                    decision.reason,
                    quote.last,
                    decision.features.score,
                    decision.features.trade_flow,
                    self.ledger.position,
                    self.ledger.mark_to_market(quote.last),
                    self.gateway.snapshot(),
                )
            except Exception:
                LOG.exception("strategy cycle failed; no additional order is sent in this cycle")

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)
        if hasattr(self.gateway, "cancel_all"):
            self.gateway.cancel_all()
        self.recorder.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TTB independent-fill simulator strategy")
    parser.add_argument("--symbol", required=True, help="TTB contract code, e.g. TXFA7")
    parser.add_argument("--mode", choices=("shadow", "ttb"), default="shadow")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--point-value", type=float, default=200.0)
    parser.add_argument("--fee-per-fill", type=float, default=0.0)
    parser.add_argument("--tax-rate", type=float, default=0.0)
    parser.add_argument("--slippage-ticks", type=float, default=0.0)
    parser.add_argument("--target-ticks", type=int, default=3)
    parser.add_argument("--stop-ticks", type=int, default=8)
    parser.add_argument("--max-hold-seconds", type=float, default=15.0)
    parser.add_argument("--signal-threshold", type=float, default=0.16)
    parser.add_argument("--base-qty", type=int, default=1)
    parser.add_argument("--max-order-qty", type=int, default=4)
    parser.add_argument("--max-inventory", type=int, default=8)
    parser.add_argument("--recovery-fraction", type=float, default=0.25)
    parser.add_argument("--daily-loss-limit", type=float, default=50_000.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--output", type=Path, default=Path("output") / "runs")
    parser.add_argument(
        "--confirm-virtual-orders",
        action="store_true",
        help="required with --mode ttb; confirms that orders may be sent to the logged-in virtual account",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.mode == "ttb" and not args.confirm_virtual_orders:
        raise SystemExit("--mode ttb requires --confirm-virtual-orders")

    config = StrategyConfig(
        symbol=args.symbol.upper(),
        tick_size=args.tick_size,
        point_value=args.point_value,
        fee_per_fill=args.fee_per_fill,
        tax_rate=args.tax_rate,
        slippage_ticks=args.slippage_ticks,
        target_ticks=args.target_ticks,
        stop_ticks=args.stop_ticks,
        max_hold_seconds=args.max_hold_seconds,
        signal_threshold=args.signal_threshold,
        base_qty=args.base_qty,
        max_order_qty=args.max_order_qty,
        max_inventory=args.max_inventory,
        recovery_fraction=args.recovery_fraction,
        daily_loss_limit=args.daily_loss_limit,
    )
    config.validate()
    LOG.info(
        "symbol=%s mode=%s target=%s ticks estimated_min_ticks=%s cycle_net_at_40000=%.2f",
        config.symbol,
        args.mode,
        config.target_ticks,
        config.minimum_profitable_ticks(40_000),
        config.estimated_cycle_net(40_000),
    )

    from TTBHelp import TTBModule

    ledger = PositionLedger(config)

    class TTBProcess(TTBModule):
        def SHOWQUOTEDATA(self, obj: dict[str, Any]) -> None:
            pass

    module = TTBProcess(args.host, args.zmq_port)
    gateway = (
        ShadowGateway(ledger)
        if args.mode == "shadow"
        else TTBOrderGateway(module, config, ledger, args.poll_seconds)
    )
    engine = BotEngine(config, gateway, args.output)
    subscriber = ZmqQuoteSubscriber(args.zmq_port, config.symbol, engine.submit)

    def stop_handler(_signum: int, _frame: Any) -> None:
        engine.stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    engine.start()
    subscriber.start()
    LOG.info("subscribed; press Ctrl+C to stop")
    try:
        while not engine.stop_event.wait(1.0):
            pass
    finally:
        subscriber.close()
        engine.close()
        LOG.info(
            "finished realized=%.2f fees_tax=%.2f position=%s output=%s",
            ledger.realized_pnl,
            ledger.fees_and_tax,
            ledger.position,
            engine.recorder.path,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
