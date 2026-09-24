from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from strategy_core import Quote

from .api import TTBApi


LOG = logging.getLogger("ttb.market_data")


class ZmqQuoteSubscriber:
    """Stoppable version of the quote loop bundled in TTBHelp.pyc."""

    def __init__(self, port: int, symbol: str, callback: Callable[[dict[str, Any]], None]) -> None:
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
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.socket.close(linger=0)
        self.context.term()


class QuoteMonitor:
    """Registers a TTB symbol and emits validated, typed ``Quote`` objects."""

    def __init__(
        self,
        api: TTBApi,
        port: int,
        symbol: str,
        callback: Callable[[Quote], None],
        subscriber_factory: Callable[..., ZmqQuoteSubscriber] = ZmqQuoteSubscriber,
    ) -> None:
        self.api = api
        self.symbol = symbol.upper()
        self.callback = callback
        self.subscriber = subscriber_factory(port, self.symbol, self.feed)

    def feed(self, raw: dict[str, Any]) -> None:
        """Parse one message; public to support replay and unit testing."""
        quote = Quote.from_ttb(raw)
        if quote.symbol and quote.symbol != self.symbol:
            return
        if not quote.is_valid():
            LOG.warning("ignored invalid quote for %s: %s", self.symbol, raw)
            return
        self.callback(quote)

    def start(self) -> None:
        # A SUB socket receives nothing until TTB has registered the symbol.
        self.api.subscribe_quote(self.symbol)
        self.subscriber.start()
        LOG.info("requested and subscribed to TTB quote %s", self.symbol)

    def close(self) -> None:
        self.subscriber.close()
