from __future__ import annotations

import csv
import json
import logging
import queue
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from strategy_core import MicropriceMarketMaker, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision

from .orders import OrderGateway
from .health import HealthReporter


LOG = logging.getLogger("ttb.engine")


class Strategy(Protocol):
    """The only interface a custom strategy must implement."""

    def decide(self, quote: Quote) -> StrategyDecision: ...


class CsvRecorder:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"ttb-shadow-{stamp}.csv"
        self._stream = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(
            self._stream,
            fieldnames=[
                "wall_time", "exchange_time", "received_at", "total_volume", "symbol", "bid", "ask", "last",
                "last_qty", "bid_qty", "ask_qty", "score", "trade_flow", "position", "average_price",
                "realized_pnl", "mark_to_market", "decision", "active_orders",
            ],
        )
        self._writer.writeheader()

    def write(
        self,
        quote: Quote,
        decision: StrategyDecision,
        ledger: PositionLedger,
        orders: dict[str, Any],
    ) -> None:
        self._writer.writerow(
            {
                "wall_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "exchange_time": quote.exchange_time,
                "received_at": quote.received_at,
                "total_volume": quote.total_volume,
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
        if not self._stream.closed:
            self._stream.close()


class BotEngine:
    """Connect a strategy to any order gateway.

    Strategy code only returns ``OrderIntent`` values.  Switching from shadow
    to TTB therefore changes the gateway, not the strategy itself.
    """

    def __init__(
        self,
        config: StrategyConfig,
        gateway: OrderGateway,
        output: Path,
        strategy: Strategy | None = None,
        health_path: Path | None = None,
        quote_stale_seconds: float = 5.0,
        resume_fresh_ticks: int = 3,
    ) -> None:
        self.config = config
        self.ledger = gateway.ledger
        self.strategy = strategy or MicropriceMarketMaker(config, self.ledger)
        self.gateway = gateway
        self.recorder = CsvRecorder(output)
        self.queue: queue.Queue[Quote] = queue.Queue(maxsize=20_000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="strategy-engine", daemon=True)
        self.last_decision_at = 0.0
        self.last_quote: Quote | None = None
        self.started_at = time.monotonic()
        self.quote_stale_seconds = max(2.0, quote_stale_seconds)
        self.resume_fresh_ticks = max(1, resume_fresh_ticks)
        self.market_data_stale = False
        self.fresh_after_stale = 0
        self.closed_cleanup_done = False
        self.fatal_error = ""
        self.health = HealthReporter(
            health_path or output / "health.json",
            symbol=config.symbol,
            mode="unknown",
            process_id=__import__("os").getpid(),
            position=0,
            clean_shutdown=False,
        )
        self.raw_stream = (output / f"quotes-{time.strftime('%Y%m%d-%H%M%S')}.csv").open(
            "w", newline="", encoding="utf-8-sig"
        )
        self.raw_writer = csv.DictWriter(self.raw_stream, fieldnames=list(Quote.__dataclass_fields__))
        self.raw_writer.writeheader()

    def start(self) -> None:
        self.health.update(state="running")
        self.thread.start()

    @staticmethod
    def _session_phase(local: datetime | None = None) -> str:
        local = local or datetime.now(timezone(timedelta(hours=8)))
        if local.weekday() >= 5:
            return "closed"
        clock = local.strftime("%H:%M:%S")
        return "trading" if "08:45:00" <= clock < "13:45:00" else "closed"

    def submit(self, value: Quote | dict[str, Any]) -> None:
        quote = value if isinstance(value, Quote) else Quote.from_ttb(value)
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

    def process_quote(self, quote: Quote) -> StrategyDecision | None:
        """Run one cycle synchronously; useful for tests and custom runners."""
        if time.monotonic() - quote.received_at > 2:
            raise RuntimeError("Quote backlog exceeds 2 seconds")
        self.last_quote = quote
        self.raw_writer.writerow(asdict(quote))
        self.raw_stream.flush()
        self.gateway.before_decision(quote)
        decision = self.strategy.decide(quote)
        local = datetime.now(timezone(timedelta(hours=8)))
        clock = local.strftime("%H:%M")
        if self.config.virtual_max_mode:
            entry_start, entry_cutoff, flatten_time = "08:45", "13:35", "13:42"
        else:
            entry_start, entry_cutoff, flatten_time = "08:55", "13:20", "13:35"
        if local.weekday() >= 5 or clock >= flatten_time or clock < entry_start:
            position = self.ledger.position
            intents = (
                OrderIntent(
                    "sell" if position > 0 else "buy",
                    quote.bid if position > 0 else quote.ask,
                    abs(position),
                    "risk_exit",
                    "session close",
                ),
            ) if position else ()
            decision = StrategyDecision(intents, decision.features, True, "outside entry session")
        elif clock >= entry_cutoff and not self.ledger.position:
            decision = StrategyDecision((), decision.features, True, "entry cutoff")
        if self.market_data_stale:
            self.fresh_after_stale += 1
            if self.ledger.position:
                decision = StrategyDecision(
                    (
                        OrderIntent(
                            "sell" if self.ledger.position > 0 else "buy",
                            quote.bid if self.ledger.position > 0 else quote.ask,
                            abs(self.ledger.position),
                            "risk_exit",
                            "market data recovered; flatten",
                        ),
                    ),
                    decision.features,
                    True,
                    "market data recovered; flatten",
                )
            elif self.fresh_after_stale < self.resume_fresh_ticks:
                decision = StrategyDecision((), decision.features, True, "recovering market data")
            else:
                self.market_data_stale = False
                self.fresh_after_stale = 0
        urgent = self.ledger.position != 0 or decision.halted
        min_interval = self.config.min_quote_interval_ms / 1000.0
        if not urgent and quote.received_at - self.last_decision_at < min_interval:
            return None
        self.gateway.sync(decision.intents, quote)
        self.last_decision_at = quote.received_at
        snapshot = self.gateway.snapshot()
        self.recorder.write(quote, decision, self.ledger, snapshot)
        self.closed_cleanup_done = False
        self.health.update(
            state="healthy" if not decision.halted else "paused",
            last_quote_exchange_time=quote.exchange_time,
            last_quote_monotonic=quote.received_at,
            last_price=quote.last,
            last_decision=decision.reason,
            position=self.ledger.position,
            realized_pnl=round(self.ledger.realized_pnl, 2),
            active_orders=snapshot,
            fatal_error="",
        )
        LOG.info(
            "%s last=%.2f score=%+.3f flow=%+.3f pos=%s pnl=%.2f orders=%s",
            decision.reason,
            quote.last,
            decision.features.score,
            decision.features.trade_flow,
            self.ledger.position,
            self.ledger.mark_to_market(quote.last),
            snapshot,
        )
        return decision

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                quote = self.queue.get(timeout=0.5)
            except queue.Empty:
                now = time.monotonic()
                last_seen = self.last_quote.received_at if self.last_quote else self.started_at
                phase = self._session_phase()
                if phase == "trading" and now - last_seen > self.quote_stale_seconds:
                    if not self.market_data_stale:
                        if hasattr(self.gateway, "cancel_all"):
                            self.gateway.cancel_all()  # type: ignore[attr-defined]
                        elif self.last_quote:
                            self.gateway.sync((), self.last_quote)
                        LOG.error("Quote stream stale; entries paused until fresh ticks recover.")
                    self.market_data_stale = True
                    self.fresh_after_stale = 0
                    self.health.update(
                        state="stale_market_data",
                        position=self.ledger.position,
                        last_error="quote stream stale during trading session",
                    )
                else:
                    if phase == "closed" and not self.closed_cleanup_done and self.ledger.position == 0:
                        if hasattr(self.gateway, "cancel_all"):
                            self.gateway.cancel_all()  # type: ignore[attr-defined]
                        self.closed_cleanup_done = True
                    state = "waiting_market" if self.ledger.position == 0 else "manual_reconciliation_required"
                    self.health.update(state=state, position=self.ledger.position)
                continue
            try:
                self.process_quote(quote)
            except Exception as exc:
                self.fatal_error = str(exc)
                LOG.exception("strategy cycle failed; stopping for reconciliation")
                self.health.update(
                    state="error",
                    fatal_error=self.fatal_error,
                    position=self.ledger.position,
                )
                self.stop_event.set()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        if hasattr(self.gateway, "cancel_all"):
            self.gateway.cancel_all()  # type: ignore[attr-defined]
        self.recorder.close()
        if not self.raw_stream.closed:
            self.raw_stream.close()
        clean = not self.fatal_error and self.ledger.position == 0
        self.health.update(
            state="stopped" if clean else "manual_reconciliation_required",
            clean_shutdown=clean,
            position=self.ledger.position,
            fatal_error=self.fatal_error,
        )
