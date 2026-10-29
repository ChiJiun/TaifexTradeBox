from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy_core import PositionLedger, Quote, StrategyConfig
from ttb.engine import BotEngine
from ttb.orders import ShadowGateway
from ttb.market_data import MarketDataIntegrity, QuoteMonitor, ZmqQuoteSubscriber, exchange_seconds


TZ = timezone(timedelta(hours=8))


def quote(tick="09:00:00.000", volume=100, bid=20000, ask=20001, qty=1):
    return Quote(
        symbol="TXFA7", bid=bid, ask=ask, bid_qty=2, ask_qty=3,
        last=bid, last_qty=qty, total_volume=volume,
        exchange_time=tick, received_at=time.monotonic(),
    )


class QuoteIntegrityTests(unittest.TestCase):
    def test_parses_strict_hms(self):
        self.assertEqual(exchange_seconds("09:00:03.250"), 32403.25)
        self.assertIsNone(exchange_seconds("25:00:00"))
        self.assertIsNone(exchange_seconds("not-a-time"))

    def test_drops_duplicate_and_out_of_order_but_allows_same_ms_changed_quote(self):
        integrity = MarketDataIntegrity(
            "TXFA7", max_exchange_lag_seconds=120,
            wall_now=lambda: datetime(2026, 10, 13, 9, 0, 2, tzinfo=TZ),
        )
        first = quote(tick="09:00:00", volume=100)
        self.assertTrue(integrity.validate(first))
        self.assertFalse(integrity.validate(first))
        self.assertTrue(integrity.validate(quote(tick="09:00:00", volume=101)))
        self.assertFalse(integrity.validate(quote(tick="08:59:59", volume=102)))
        self.assertEqual(integrity.snapshot()["duplicates"], 1)
        self.assertEqual(integrity.snapshot()["out_of_order"], 1)
        self.assertEqual(integrity.snapshot()["accepted"], 2)

    def test_accepts_ttb_payload_with_exchange_suffix_on_symbol(self):
        payload = {
            "Symbol": "TMFK6.TW-B", "BidPs": 20000, "AskPs": 20001, "BidPv": 2, "AskPv": 3,
            "Price": 20000, "Qty": 1, "Volume": 100, "TickTime": "09:00:00.000",
        }
        integrity = MarketDataIntegrity("TMFK6")
        self.assertTrue(integrity.validate(Quote.from_ttb(payload)))
        self.assertEqual(integrity.snapshot()["invalid"], 0)

    def test_rejects_old_backlog_and_unparseable_timestamp_in_live_mode(self):
        integrity = MarketDataIntegrity(
            "TXFA7", max_exchange_lag_seconds=5,
            wall_now=lambda: datetime(2026, 10, 13, 9, 10, 0, tzinfo=TZ),
        )
        self.assertFalse(integrity.validate(quote(tick="09:00:00")))
        self.assertFalse(integrity.validate(quote(tick="nonsense")))
        self.assertEqual(integrity.snapshot()["accepted"], 0)
        self.assertEqual(integrity.snapshot()["lagged"], 1)
        self.assertEqual(integrity.snapshot()["unparseable_time"], 1)
        self.assertIsNone(integrity.snapshot()["last_accepted_received_at"])

    def test_rejects_unexpected_volume_reset_but_allows_session_boundary(self):
        integrity = MarketDataIntegrity("TXFA7")
        self.assertTrue(integrity.validate(quote("09:00:00", 100)))
        self.assertFalse(integrity.validate(quote("09:00:01", 50)))
        self.assertEqual(integrity.snapshot()["volume_resets"], 1)

        second = MarketDataIntegrity("TXFA7")
        self.assertTrue(second.validate(quote("13:44:59", 100)))
        self.assertTrue(second.validate(quote("15:00:01", 2)))
        self.assertEqual(second.snapshot()["volume_resets"], 1)

    def test_monitor_rejections_do_not_reach_engine_and_report_health_counters(self):
        class Api:
            def subscribe_quote(self, symbol):
                return {"Code": "0000"}
        class StubSubscriber:
            def __init__(self, port, symbol, callback):
                self.callback = callback
            def start(self):
                pass
            def close(self):
                pass
        good, statuses = [], []
        monitor = QuoteMonitor(
            Api(), 51141, "TXFA7", good.append, StubSubscriber,
            max_exchange_lag_seconds=3,
            on_status=statuses.append,
            wall_now=lambda: datetime(2026, 10, 13, 9, 0, 2, tzinfo=TZ),
        )
        def raw(ts):
            return {
                "Symbol": "TXFA7", "BidPs": "20000", "AskPs": "20001",
                "BidPv": "2", "AskPv": "3", "Price": "20000", "Qty": "1",
                "Volume": "100", "TickTime": ts,
            }
        monitor.feed(raw("09:00:00"))
        monitor.feed(raw("09:00:00"))
        monitor.feed(raw("08:50:00"))
        self.assertEqual(len(good), 1)
        self.assertEqual(monitor.integrity.snapshot()["accepted"], 1)
        self.assertEqual(monitor.integrity.snapshot()["lagged"], 1)
        self.assertTrue(statuses)

    def test_engine_burst_overload_drops_oldest_in_bounded_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            config = StrategyConfig(symbol="TXFA7")
            engine = BotEngine(config, ShadowGateway(PositionLedger(config)), Path(folder))
            for i in range(20_010):
                engine.submit(quote(volume=i, bid=20000 + i))
            self.assertEqual(engine.queue.qsize(), 20_000)
            self.assertEqual(engine.dropped_quotes, 10)
            self.assertEqual(engine.queue.get_nowait().total_volume, 10)
            engine.recorder.close()
            engine.raw_recorder.close()

    def test_engine_catches_up_to_newest_quote_after_a_slow_cycle(self):
        import dataclasses
        import threading
        with tempfile.TemporaryDirectory() as folder:
            config = StrategyConfig(symbol="TXFA7")
            engine = BotEngine(config, ShadowGateway(PositionLedger(config)), Path(folder))
            processed = []

            def record(q, now=None):
                processed.append(q.total_volume)
                engine.stop_event.set()
            engine.process_quote = record
            stale = time.monotonic() - 5
            for i in range(5):
                engine.submit(dataclasses.replace(quote(volume=i), received_at=stale))
            engine.submit(quote(volume=99))
            engine._run()
            self.assertEqual(processed, [99])
            self.assertEqual(engine.skipped_quotes, 5)

            engine.stop_event.clear()
            engine.submit(dataclasses.replace(quote(volume=7), received_at=stale))
            timer = threading.Timer(1.0, engine.stop_event.set)
            timer.start()
            engine._run()
            timer.join()
            self.assertEqual(processed, [99])
            self.assertEqual(engine.skipped_quotes, 6)
            self.assertEqual(engine.fatal_error, "")
            engine.recorder.close()
            engine.raw_recorder.close()

    def test_zmq_subscriber_uses_finite_hwm_and_exact_topic(self):
        import zmq
        subscriber = ZmqQuoteSubscriber(59989, "TXFA7", lambda _data: None)
        try:
            self.assertEqual(subscriber.topic, "TTB$Tick$TXFA7")
            self.assertEqual(subscriber.socket.getsockopt(zmq.RCVHWM), 1000)
        finally:
            subscriber.close()

    def test_zmq_subscriber_accepts_suffixed_topic_but_not_other_contracts(self):
        import json
        import zmq
        context = zmq.Context.instance()
        publisher = context.socket(zmq.PUB)
        port = publisher.bind_to_random_port("tcp://127.0.0.1")
        received = []
        subscriber = ZmqQuoteSubscriber(port, "TMFK6", received.append)
        subscriber.start()
        try:
            deadline = time.monotonic() + 5
            while not received and time.monotonic() < deadline:
                publisher.send_multipart([b"TTB$Tick$TMFK60", json.dumps({"Symbol": "TMFK60"}).encode()])
                publisher.send_multipart([b"TTB$Tick$TMFK6.TW-B", json.dumps({"Symbol": "TMFK6.TW-B"}).encode()])
                time.sleep(0.05)
            self.assertTrue(received, "suffixed topic delivered")
            self.assertEqual({row["Symbol"] for row in received}, {"TMFK6.TW-B"})
        finally:
            subscriber.close()
            publisher.close(linger=0)


if __name__ == "__main__":
    unittest.main()
