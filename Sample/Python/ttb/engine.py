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
from .runtime import RiskStateStore, SessionSchedule, TAIPEI_TZ


LOG = logging.getLogger("ttb.engine")
# A quote older than this is no longer a price to trade on.
MAX_QUOTE_AGE_SECONDS = 2.0
# Lagging the feed by more than this means a slow cycle; jump to the newest tick.
QUOTE_CATCH_UP_SECONDS = 0.5


class Strategy(Protocol):
    """The only interface a custom strategy must implement."""

    def decide(self, quote: Quote) -> StrategyDecision: ...


class DailyCsvWriter:
    """Rotate CSV output at Taipei calendar-day boundaries."""

    def __init__(self, directory: Path, prefix: str, fieldnames: list[str]) -> None:
        self.directory = directory
        self.prefix = prefix
        self.fieldnames = fieldnames
        self.directory.mkdir(parents=True, exist_ok=True)
        self._day = ""
        self._stream: Any = None
        self._writer: csv.DictWriter | None = None
        self.path = Path()
        self._ensure_open()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(TAIPEI_TZ)

    def _ensure_open(self) -> None:
        now = self._now()
        day = now.strftime("%Y%m%d")
        if self._writer is not None and day == self._day:
            return
        if self._stream is not None and not self._stream.closed:
            self._stream.close()
        self._day = day
        stamp = now.strftime("%Y%m%d-%H%M%S")
        self.path = self.directory / f"{self.prefix}-{stamp}.csv"
        self._stream = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._stream, fieldnames=self.fieldnames)
        self._writer.writeheader()
        self._stream.flush()

    def write(self, row: dict[str, Any]) -> None:
        self._ensure_open()
        assert self._writer is not None
        self._writer.writerow(row)
        self._stream.flush()

    def close(self) -> None:
        if self._stream is not None and not self._stream.closed:
            self._stream.close()


class CsvRecorder:
    def __init__(self, directory: Path) -> None:
        self._sink = DailyCsvWriter(
            directory,
            "ttb-shadow",
            [
                "wall_time", "exchange_time", "received_at", "total_volume", "symbol", "bid", "ask", "last",
                "last_qty", "bid_qty", "ask_qty", "score", "trade_flow", "position", "average_price",
                "realized_pnl", "mark_to_market", "decision", "active_orders",
            ],
        )

    @property
    def path(self) -> Path:
        return self._sink.path

    def write(
        self,
        quote: Quote,
        decision: StrategyDecision,
        ledger: PositionLedger,
        orders: dict[str, Any],
    ) -> None:
        self._sink.write(
            {
                "wall_time": datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
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

    def close(self) -> None:
        self._sink.close()


class RawQuoteRecorder:
    def __init__(self, directory: Path) -> None:
        self._sink = DailyCsvWriter(directory, "quotes", list(Quote.__dataclass_fields__))

    @property
    def path(self) -> Path:
        return self._sink.path

    def write(self, quote: Quote) -> None:
        self._sink.write(asdict(quote))

    def close(self) -> None:
        self._sink.close()


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
        stale_fatal_seconds: float = 120.0,
        resume_fresh_ticks: int = 3,
        session_schedule: SessionSchedule | None = None,
        risk_state_path: Path | None = None,
        flatten_on_shutdown: bool = False,
        feed_dead_seconds: float = 0.0,
    ) -> None:
        self.config = config
        self.ledger = gateway.ledger
        self.strategy = strategy or MicropriceMarketMaker(config, self.ledger)
        self.gateway = gateway
        self.recorder = CsvRecorder(output)
        self.queue: queue.Queue[Quote] = queue.Queue(maxsize=20_000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run_guarded, name="strategy-engine", daemon=True)
        self.last_decision_at = 0.0
        self.last_quote: Quote | None = None
        self.started_at = time.monotonic()
        self.quote_stale_seconds = max(2.0, quote_stale_seconds)
        self.stale_fatal_seconds = max(0.0, stale_fatal_seconds)
        self.resume_fresh_ticks = max(1, resume_fresh_ticks)
        self.market_data_stale = False
        self.stale_since = 0.0
        self.fresh_after_stale = 0
        self.submitted_quotes = 0
        self.processed_quotes = 0
        self.dropped_quotes = 0
        self.skipped_quotes = 0
        self.decision_count = 0
        self.first_quote_grace_seconds = max(30.0, self.quote_stale_seconds * 3.0)
        self.feed_dead_seconds = max(0.0, feed_dead_seconds)
        self.feed_seen_at = self.started_at
        self.closed_cleanup_done = False
        self.fatal_error = ""
        self.session_schedule = session_schedule or SessionSchedule()
        self.risk_state = RiskStateStore(
            risk_state_path or output.parent / "risk-state.json",
            config.symbol,
        )
        self.current_trading_day = ""
        self._risk_state_ready = False
        self._previous_halt_reason = ""
        self.flatten_on_shutdown = flatten_on_shutdown
        self.health = HealthReporter(
            health_path or output / "health.json",
            symbol=config.symbol,
            mode="unknown",
            process_id=__import__("os").getpid(),
            position=0,
            clean_shutdown=False,
        )
        self.raw_recorder = RawQuoteRecorder(output)

    def start(self) -> None:
        self.health.update(state="running")
        self.thread.start()

    @staticmethod
    def _session_phase(local: datetime | None = None) -> str:
        """Backward-compatible coarse phase used by older tests/callers."""
        return "trading" if SessionSchedule().status(local).trading else "closed"

    def _restore_or_roll_risk_state(self, local: datetime) -> None:
        status = self.session_schedule.status(local)
        trading_day = status.trading_day
        if trading_day == self.current_trading_day:
            return
        self.current_trading_day = trading_day
        self._risk_state_ready = False
        saved = self.risk_state.load(trading_day)
        if hasattr(self.gateway, "set_trading_day"):
            self.gateway.set_trading_day(trading_day)  # type: ignore[attr-defined]
        if saved:
            self.ledger.realized_pnl = float(saved.get("realized_pnl", 0.0))
            self.ledger.peak_realized_pnl = float(saved.get("peak_realized_pnl", self.ledger.realized_pnl))
            self.ledger.fees_and_tax = float(saved.get("fees_and_tax", 0.0))
            self.ledger.fill_count = int(saved.get("fill_count", 0))
            if hasattr(self.strategy, "peak_equity"):
                self.strategy.peak_equity = float(saved.get("peak_equity", 0.0))
            if hasattr(self.strategy, "halt_reason"):
                self.strategy.halt_reason = str(saved.get("halt_reason", ""))
            strategy_state = saved.get("strategy_state", {})
            for name in ("win_streak", "loss_streak", "flat_realized_baseline", "last_cycle_net"):
                if hasattr(self.strategy, name) and name in strategy_state:
                    setattr(self.strategy, name, strategy_state[name])
            if hasattr(self.strategy, "observed_position"):
                self.strategy.observed_position = self.ledger.position
            LOG.warning("restored same-session risk state for %s", trading_day)
        else:
            # A new trading day resets only cumulative session accounting.
            self.ledger.realized_pnl = 0.0
            self.ledger.peak_realized_pnl = 0.0
            self.ledger.fees_and_tax = 0.0
            self.ledger.fill_count = 0
            if hasattr(self.strategy, "peak_equity"):
                self.strategy.peak_equity = 0.0
            if hasattr(self.strategy, "halt_reason"):
                self.strategy.halt_reason = ""
            for name, value in (
                ("win_streak", 0),
                ("loss_streak", 0),
                ("flat_realized_baseline", 0.0),
                ("last_cycle_net", 0.0),
            ):
                if hasattr(self.strategy, name):
                    setattr(self.strategy, name, value)

        # The durable fill stream is authoritative after a crash between
        # broker fill, ledger update and risk-state checkpoint. An incomplete
        # historic state is never silently reset when the journal is empty.
        if hasattr(self.gateway, "journal"):
            journal = self.gateway.journal  # type: ignore[attr-defined]
            recovered, _cursors = journal.replay_ledger(trading_day, self.config)
            for event in journal.events(trading_day):
                if event["kind"] == "risk_halt_transition" and hasattr(self.strategy, "halt_reason"):
                    self.strategy.halt_reason = str(event["payload"].get("current", ""))
            if recovered.position != 0:
                raise RuntimeError("execution journal has open position; manual TTB reconciliation required")
            if saved and int(saved.get("fill_count", 0)) > recovered.fill_count:
                raise RuntimeError("risk snapshot contains fills absent from journal; manual reconciliation required")
            self.ledger.realized_pnl = recovered.realized_pnl
            self.ledger.peak_realized_pnl = recovered.peak_realized_pnl
            self.ledger.fees_and_tax = recovered.fees_and_tax
            self.ledger.fill_count = recovered.fill_count
            if self.ledger.realized_pnl <= -self.config.daily_loss_limit:
                if hasattr(self.strategy, "halt_reason"):
                    self.strategy.halt_reason = "journal_recovered_daily_loss_limit"
            elif (
                self.ledger.peak_realized_pnl - self.ledger.realized_pnl
                >= self.config.max_drawdown
            ):
                if hasattr(self.strategy, "halt_reason"):
                    self.strategy.halt_reason = "journal_recovered_max_drawdown"
            self._previous_halt_reason = str(getattr(self.strategy, "halt_reason", ""))
        self._risk_state_ready = True

    def note_feed_tick(self) -> None:
        """Record a tick of any symbol on TTB's quote feed (subscriber thread)."""
        self.feed_seen_at = time.monotonic()

    def _stop_on_dead_feed(self, silent: float) -> None:
        # Every symbol TTB publishes, including the liquid canary the
        # supervisor subscribes, went quiet: TTB's quote feed died and only a
        # TTB restart brings it back, so stop instead of waiting out the
        # per-contract stale windows that sparse contracts need.
        if hasattr(self.gateway, "cancel_all"):
            self.gateway.cancel_all()  # type: ignore[attr-defined]
        self.fatal_error = (
            f"TTB quote feed silent for {silent:.1f}s "
            f"(feed dead threshold {self.feed_dead_seconds:.1f}s); restart TTB"
        )
        LOG.error(self.fatal_error)
        self.health.update(state="error", fatal_error=self.fatal_error, position=self.ledger.position)
        self.stop_event.set()

    def _checkpoint_risk_state(self) -> None:
        if not self.current_trading_day or not self._risk_state_ready:
            return
        strategy_state = {
            name: getattr(self.strategy, name)
            for name in ("win_streak", "loss_streak", "flat_realized_baseline", "last_cycle_net")
            if hasattr(self.strategy, name)
        }
        self.risk_state.checkpoint(
            trading_day=self.current_trading_day,
            realized_pnl=self.ledger.realized_pnl,
            peak_realized_pnl=self.ledger.peak_realized_pnl,
            fees_and_tax=self.ledger.fees_and_tax,
            fill_count=self.ledger.fill_count,
            peak_equity=float(getattr(self.strategy, "peak_equity", 0.0)),
            halt_reason=str(getattr(self.strategy, "halt_reason", "")),
            position=self.ledger.position,
            strategy_state=strategy_state,
        )

    def submit(self, value: Quote | dict[str, Any]) -> None:
        quote = value if isinstance(value, Quote) else Quote.from_ttb(value)
        if quote.symbol and quote.symbol != self.config.symbol.upper():
            return
        self.submitted_quotes += 1
        try:
            self.queue.put_nowait(quote)
        except queue.Full:
            self.dropped_quotes += 1
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(quote)

    def process_quote(self, quote: Quote, now: float | None = None) -> StrategyDecision | None:
        """Run one cycle synchronously; useful for tests and custom runners."""
        if (time.monotonic() if now is None else now) - quote.received_at > MAX_QUOTE_AGE_SECONDS:
            raise RuntimeError(f"Quote backlog exceeds {MAX_QUOTE_AGE_SECONDS:g} seconds")
        self.last_quote = quote
        self.processed_quotes += 1
        self.raw_recorder.write(quote)
        local = datetime.now(TAIPEI_TZ)
        self._restore_or_roll_risk_state(local)
        self.gateway.before_decision(quote)
        decision = self.strategy.decide(quote)
        session = self.session_schedule.status(local)
        entry_delay_minutes = 0.0 if self.config.virtual_max_mode else 10.0
        entry_cutoff_minutes = 10.0 if self.config.virtual_max_mode else 25.0
        flatten_buffer_minutes = 3.0 if self.config.virtual_max_mode else 10.0
        if not session.trading or session.minutes_until_close <= flatten_buffer_minutes:
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
        elif getattr(session, "reason", "") == "contract_expiry_today":
            position = self.ledger.position
            intents = (
                OrderIntent(
                    "sell" if position > 0 else "buy",
                    quote.bid if position > 0 else quote.ask,
                    abs(position), "risk_exit", "contract expiry",
                ),
            ) if position else ()
            decision = StrategyDecision(
                intents, decision.features, True,
                "expiry day: exit only; manual rollover required",
            )
        elif session.minutes_since_open < entry_delay_minutes and not self.ledger.position:
            decision = StrategyDecision((), decision.features, True, "session opening delay")
        elif session.minutes_until_close <= entry_cutoff_minutes and not self.ledger.position:
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
                self.stale_since = 0.0
                self.fresh_after_stale = 0
        urgent = self.ledger.position != 0 or decision.halted
        min_interval = self.config.min_quote_interval_ms / 1000.0
        if not urgent and quote.received_at - self.last_decision_at < min_interval:
            return None
        halt_reason = str(getattr(self.strategy, "halt_reason", ""))
        if hasattr(self.gateway, "record_event") and halt_reason != self._previous_halt_reason:
            self.gateway.record_event(  # type: ignore[attr-defined]
                "risk_halt_transition",
                {"previous": self._previous_halt_reason, "current": halt_reason,
                 "position": self.ledger.position, "realized_pnl": self.ledger.realized_pnl},
            )
            self._previous_halt_reason = halt_reason
        self.gateway.sync(decision.intents, quote)
        self.last_decision_at = quote.received_at
        self.decision_count += 1
        snapshot = self.gateway.snapshot()
        self.recorder.write(quote, decision, self.ledger, snapshot)
        self.closed_cleanup_done = False
        self._checkpoint_risk_state()
        self.health.update(
            state="healthy" if not decision.halted else "paused",
            session_phase=session.phase,
            trading_day=session.trading_day,
            calendar_reason=getattr(session, "reason", ""),
            last_quote_exchange_time=quote.exchange_time,
            last_quote_monotonic=quote.received_at,
            # Heartbeats are refreshed independently while waiting for ticks.
            # Keep the actual accepted quote wall time separate, so portfolio
            # risk monitors cannot mistake stale prices for fresh valuations.
            last_quote_wall_utc=datetime.now(timezone.utc).isoformat(),
            last_price=quote.last,
            point_value=self.config.point_value,
            marked_pnl=round(
                self.ledger.mark_to_market(
                    quote.bid if self.ledger.position > 0 else
                    quote.ask if self.ledger.position < 0 else quote.last
                )
                - abs(self.ledger.position) * (
                    self.config.fee_per_fill
                    + abs(quote.last * self.config.point_value * self.config.tax_rate)
                    + self.config.slippage_ticks * self.config.tick_value
                ), 2,
            ),
            last_decision=decision.reason,
            position=self.ledger.position,
            realized_pnl=round(self.ledger.realized_pnl, 2),
            active_orders=snapshot,
            queue_depth=self.queue.qsize(),
            submitted_quotes=self.submitted_quotes,
            processed_quotes=self.processed_quotes,
            dropped_quotes=self.dropped_quotes,
            skipped_quotes=self.skipped_quotes,
            decision_count=self.decision_count,
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

    def _run_guarded(self) -> None:
        # Even disk-full failures in the health reporter must stop the main
        # process. Otherwise the worker thread can die while main() waits
        # forever on an event that was never set.
        try:
            self._run()
        except BaseException as exc:
            self.fatal_error = self.fatal_error or f"uncaught strategy worker failure: {exc}"
            LOG.exception("strategy worker crashed; forcing fail-closed shutdown")
            try:
                self.health.update(state="error", fatal_error=self.fatal_error)
            except Exception:
                LOG.exception("cannot persist failure to health file")
            finally:
                self.stop_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                quote = self.queue.get(timeout=0.5)
            except queue.Empty:
                now = time.monotonic()
                last_seen = self.last_quote.received_at if self.last_quote else self.started_at
                session = self.session_schedule.status()
                phase = "trading" if session.trading else "closed"
                has_seen_quote = self.last_quote is not None
                if phase != "trading":
                    # The silence window starts at the open, not at launch.
                    self.feed_seen_at = now
                elif self.feed_dead_seconds and now - self.feed_seen_at > self.feed_dead_seconds:
                    self._stop_on_dead_feed(now - self.feed_seen_at)
                    continue
                no_first_quote = (
                    phase == "trading"
                    and not has_seen_quote
                    and now - self.started_at > self.first_quote_grace_seconds
                )
                if phase == "trading" and ((has_seen_quote and now - last_seen > self.quote_stale_seconds) or no_first_quote):
                    if not self.market_data_stale:
                        if hasattr(self.gateway, "cancel_all"):
                            self.gateway.cancel_all()  # type: ignore[attr-defined]
                        elif self.last_quote:
                            self.gateway.sync((), self.last_quote)
                        LOG.error("Quote stream stale; entries paused until fresh ticks recover.")
                    self.market_data_stale = True
                    if not self.stale_since:
                        self.stale_since = now
                    self.fresh_after_stale = 0
                    stale_duration = max(0.0, now - self.stale_since)
                    self.health.update(
                        state="stale_market_data",
                        session_phase=session.phase,
                        trading_day=session.trading_day,
                        position=self.ledger.position,
                        last_error=(
                            "no first quote received during trading session"
                            if no_first_quote
                            else "quote stream stale during trading session"
                        ),
                        queue_depth=self.queue.qsize(),
                        submitted_quotes=self.submitted_quotes,
                        processed_quotes=self.processed_quotes,
                        dropped_quotes=self.dropped_quotes,
                        stale_duration_seconds=round(stale_duration, 1),
                    )
                    # A feed that never delivers a first tick is as dead as one
                    # that stopped; only a restart of TTB resubscribes it.
                    if self.stale_fatal_seconds > 0 and stale_duration >= self.stale_fatal_seconds:
                        self.fatal_error = (
                            f"market data stale for {stale_duration:.1f}s "
                            f"(fatal threshold {self.stale_fatal_seconds:.1f}s)"
                        )
                        LOG.error(self.fatal_error)
                        self.health.update(
                            state="error",
                            fatal_error=self.fatal_error,
                            position=self.ledger.position,
                        )
                        self.stop_event.set()
                else:
                    if phase == "closed" and not self.closed_cleanup_done and self.ledger.position == 0:
                        if hasattr(self.gateway, "cancel_all"):
                            self.gateway.cancel_all()  # type: ignore[attr-defined]
                        self.closed_cleanup_done = True
                    manual_required = getattr(session, "reason", "") in {
                        "calendar_unverified", "manual_contract_rollover_required",
                        "contract_expiry_today",
                    }
                    state = (
                        "manual_reconciliation_required" if manual_required or self.ledger.position != 0
                        else "waiting_market"
                    )
                    self.health.update(
                        state=state,
                        session_phase=session.phase,
                        trading_day=session.trading_day,
                        calendar_reason=getattr(session, "reason", ""),
                        position=self.ledger.position,
                    )
                continue
            # Live broker polls can outlast several ticks. Once behind, act on
            # the newest tick (orders priced off older ones are already stale)
            # and skip one too old to trade on instead of failing the session.
            while time.monotonic() - quote.received_at > QUOTE_CATCH_UP_SECONDS:
                try:
                    newer = self.queue.get_nowait()
                except queue.Empty:
                    break
                self.raw_recorder.write(quote)
                self.skipped_quotes += 1
                quote = newer
            now = time.monotonic()
            if now - quote.received_at > MAX_QUOTE_AGE_SECONDS:
                self.raw_recorder.write(quote)
                self.skipped_quotes += 1
                continue
            try:
                # Same clock reading, so a tick cannot pass here and age out inside.
                self.process_quote(quote, now)
            except Exception as exc:
                self.fatal_error = str(exc)
                LOG.exception("strategy cycle failed; stopping for reconciliation")
                gateway_snapshot = self.gateway.snapshot()
                self.health.update(
                    state="error",
                    fatal_error=self.fatal_error,
                    position=self.ledger.position,
                    active_orders=gateway_snapshot,
                    risk_block_reason=gateway_snapshot.get("risk_block_reason", ""),
                )
                self.stop_event.set()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        shutdown_reconciled = True
        session = self.session_schedule.status()
        if (
            self.flatten_on_shutdown
            and self.ledger.position
            and self.last_quote is not None
            and session.trading
            and hasattr(self.gateway, "emergency_flatten")
        ):
            try:
                shutdown_reconciled = bool(self.gateway.emergency_flatten(self.last_quote))  # type: ignore[attr-defined]
            except Exception:
                shutdown_reconciled = False
                LOG.exception("graceful shutdown flatten failed")
        if hasattr(self.gateway, "finalize_shutdown"):
            try:
                shutdown_reconciled = bool(self.gateway.finalize_shutdown()) and shutdown_reconciled  # type: ignore[attr-defined]
            except Exception:
                shutdown_reconciled = False
                LOG.exception("final shutdown reconciliation failed")
        elif hasattr(self.gateway, "cancel_all"):
            self.gateway.cancel_all()  # type: ignore[attr-defined]
        try:
            if hasattr(self.gateway, "record_event"):
                self.gateway.record_event(  # type: ignore[attr-defined]
                    "shutdown", {"position": self.ledger.position,
                                 "fatal_error": self.fatal_error,
                                 "reconciled": shutdown_reconciled},
                )
        except Exception as exc:
            self.fatal_error = self.fatal_error or str(exc)
            shutdown_reconciled = False
            LOG.exception("shutdown journal persistence failed")
        self._checkpoint_risk_state()
        self.recorder.close()
        self.raw_recorder.close()
        clean = not self.fatal_error and self.ledger.position == 0 and shutdown_reconciled
        self.health.update(
            state="stopped" if clean else "manual_reconciliation_required",
            clean_shutdown=clean,
            position=self.ledger.position,
            fatal_error=self.fatal_error,
        )
