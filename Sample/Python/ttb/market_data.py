from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from strategy_core import Quote
from .api import TTBApi


LOG = logging.getLogger("ttb.market_data")
TAIPEI_TZ = timezone(timedelta(hours=8))


def exchange_seconds(text: str) -> float | None:
    """Accept only documented-style HH:MM:SS[.fraction] timestamps."""
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", text):
        return None
    try:
        timestamp = datetime.strptime(text, "%H:%M:%S.%f" if "." in text else "%H:%M:%S")
    except ValueError:
        return None
    return timestamp.hour * 3600 + timestamp.minute * 60 + timestamp.second + timestamp.microsecond / 1e6


class MarketDataIntegrity:
    """Deterministic rejection of stale, duplicated and reversed live ticks."""

    def __init__(
        self, symbol: str, max_exchange_lag_seconds: float | None = None,
        wall_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.max_exchange_lag_seconds = max_exchange_lag_seconds
        self.wall_now = wall_now or (lambda: datetime.now(TAIPEI_TZ))
        self.counters: dict[str, int] = {
            "received": 0, "accepted": 0, "invalid": 0,
            "duplicates": 0, "out_of_order": 0, "volume_resets": 0,
            "lagged": 0, "unparseable_time": 0,
        }
        self._last_tick: Quote | None = None
        self._last_seconds: float | None = None
        self._last_wall: datetime | None = None

    def validate(self, quote: Quote) -> bool:
        self.counters["received"] += 1
        if (quote.symbol and quote.symbol.upper() != self.symbol) or not quote.is_valid():
            self.counters["invalid"] += 1
            return False
        now = self.wall_now().astimezone(TAIPEI_TZ)
        seconds = exchange_seconds(quote.exchange_time)
        if seconds is None:
            self.counters["unparseable_time"] += 1
            # Unknown exchange time means stale/backlog safety cannot be proven
            # in live mode; other unit-test integrations can explicitly omit
            # the lag check pending #2 TTB format verification.
            if self.max_exchange_lag_seconds is not None:
                return False
        elif self.max_exchange_lag_seconds is not None:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            candidates = [
                midnight + timedelta(days=day_offset, seconds=seconds)
                for day_offset in (-1, 0, 1)
            ]
            candidate = min(candidates, key=lambda value: abs((now - value).total_seconds()))
            lag = (now - candidate).total_seconds()
            if lag > self.max_exchange_lag_seconds or lag < -2:
                self.counters["lagged"] += 1
                return False

        previous = self._last_tick
        old_seconds = self._last_seconds
        same_day = self._last_wall is not None and self._last_wall.date() == now.date()
        midnight_roll = (
            seconds is not None and old_seconds is not None
            and old_seconds >= 23 * 3600 and seconds < 5 * 3600
        )
        if previous:
            identical = (
                quote.exchange_time == previous.exchange_time
                and quote.total_volume == previous.total_volume
                and quote.bid == previous.bid and quote.ask == previous.ask
                and quote.bid_qty == previous.bid_qty and quote.ask_qty == previous.ask_qty
                and quote.last == previous.last and quote.last_qty == previous.last_qty
            )
            if identical:
                self.counters["duplicates"] += 1
                return False
            if same_day and not midnight_roll and (
                seconds is not None and old_seconds is not None and seconds < old_seconds
            ):
                self.counters["out_of_order"] += 1
                return False
            if quote.total_volume < previous.total_volume:
                session_reset = (
                    seconds is not None and old_seconds is not None
                    and (
                        (old_seconds < 14 * 3600 and seconds >= 15 * 3600)
                        or (old_seconds < 5 * 3600 and seconds >= 8 * 3600)
                    )
                )
                if not (not same_day or midnight_roll or session_reset):
                    self.counters["volume_resets"] += 1
                    return False
                self.counters["volume_resets"] += 1

        self._last_tick = quote
        self._last_seconds = seconds
        self._last_wall = now
        self.counters["accepted"] += 1
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.counters,
            "last_accepted_exchange_time": self._last_tick.exchange_time if self._last_tick else "",
            "last_accepted_received_at": self._last_tick.received_at if self._last_tick else None,
        }


class ZmqQuoteSubscriber:
    """Bounded SUB queue, exact-symbol subscription, atomic multipart reads.

    An empty symbol subscribes to every tick TTB publishes, which tells a dead
    TTB quote feed apart from one quiet contract.
    """

    def __init__(self, port: int, symbol: str, callback: Callable[[dict[str, Any]], None]) -> None:
        import zmq

        self.zmq = zmq
        self.symbol = symbol.upper()
        self.topic = f"TTB$Tick${self.symbol}"
        self.callback = callback
        self.stop_event = threading.Event()
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        # A finite high-water mark means overflow results in dropped packets,
        # never unlimited process memory growth. Stale detection is driven by
        # successfully validated ticks, not raw received packets.
        self.socket.setsockopt(zmq.RCVHWM, 1_000)
        self.socket.setsockopt(zmq.RCVTIMEO, 500)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, self.topic)
        self.socket.connect(f"tcp://localhost:{port}")
        self.thread = threading.Thread(target=self._run, name="ttb-zmq", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frames = self.socket.recv_multipart()
            except self.zmq.Again:
                continue
            except Exception:
                if not self.stop_event.is_set():
                    LOG.exception("ZMQ quote receive failed")
                break
            if len(frames) != 2:
                LOG.warning("ignored malformed ZMQ multipart quote")
                continue
            try:
                topic = frames[0].decode("utf-8")
                if self.symbol and topic.split(".")[0] != self.topic:
                    continue
                self.callback(json.loads(frames[1]))
            except Exception:
                LOG.exception("invalid quote message")

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.socket.close(linger=0)
        self.context.term()


class QuoteMonitor:
    """TTB quote reader with exchange-time and volume integrity counters."""

    def __init__(
        self,
        api: TTBApi,
        port: int,
        symbol: str,
        callback: Callable[[Quote], None],
        subscriber_factory: Callable[..., ZmqQuoteSubscriber] = ZmqQuoteSubscriber,
        *,
        max_exchange_lag_seconds: float | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        wall_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.api = api
        self.symbol = symbol.upper()
        self.callback = callback
        self.integrity = MarketDataIntegrity(self.symbol, max_exchange_lag_seconds, wall_now)
        self.on_status = on_status
        self._last_status_emitted = 0.0
        self.subscriber = subscriber_factory(port, self.symbol, self.feed)

    def feed(self, raw: dict[str, Any]) -> None:
        try:
            quote = Quote.from_ttb(raw)
        except (ValueError, TypeError, KeyError):
            self.integrity.counters["received"] += 1
            self.integrity.counters["invalid"] += 1
            return
        valid = self.integrity.validate(quote)
        if valid:
            self.callback(quote)
        else:
            LOG.debug("dropped invalid/duplicate/stale quote for %s", self.symbol)
        now = time.monotonic()
        if self.on_status and now - self._last_status_emitted >= 1.0:
            self._last_status_emitted = now
            self.on_status(self.integrity.snapshot())

    def start(self) -> None:
        self.api.subscribe_quote(self.symbol)
        self.subscriber.start()
        LOG.info("requested and subscribed to TTB quote %s", self.symbol)

    def close(self) -> None:
        self.subscriber.close()
