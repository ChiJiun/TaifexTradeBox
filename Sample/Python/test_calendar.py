from __future__ import annotations

import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from datetime import date, datetime, timedelta, timezone
from strategy_core import Features, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision
from ttb.engine import BotEngine
from ttb.orders import ShadowGateway

from ttb.calendar import TradingCalendar, ContractInfo, CalendarCoverageError
from ttb.runtime import SessionSchedule

TZ = timezone(timedelta(hours=8))
CAL = Path(__file__).parent / "automation" / "taifex_calendar_2026.json"


class ExchangeCalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = TradingCalendar.from_file(CAL)
        self.contract = ContractInfo.from_symbol("TMFJ6", 2026)
        self.schedule = SessionSchedule(calendar=self.calendar, contract=self.contract)

    @staticmethod
    def local(month, day, hour, minute=0, year=2026):
        return datetime(year, month, day, hour, minute, tzinfo=TZ)

    def test_verified_holidays_are_closed_not_fake_trading_days(self):
        for month, day in ((1, 1), (2, 13), (4, 6), (10, 9), (10, 26), (12, 25)):
            # Contract expiry is Oct 21; for holidays after expiry use Nov.
            contract = (
                None if month == 12 else
                ContractInfo.from_symbol("TMFK6", 2026) if month == 10
                else self.contract
            )
            schedule = SessionSchedule(calendar=self.calendar, contract=contract)
            with self.subTest(month=month, day=day):
                result = schedule.status(self.local(month, day, 10))
                self.assertEqual(result.phase, "closed")
                self.assertEqual(result.reason, "exchange_holiday")

    def test_friday_night_and_saturday_early_morning_share_trading_day(self):
        friday = self.schedule.status(self.local(10, 16, 23, 59))
        saturday = self.schedule.status(self.local(10, 17, 0, 1))
        self.assertEqual(friday.phase, "after_hours")
        self.assertEqual(saturday.phase, "after_hours")
        self.assertEqual(friday.trading_day, "2026-10-19")
        self.assertEqual(saturday.trading_day, "2026-10-19")

    def test_lunar_new_year_last_night_session_is_assigned_to_first_open_day(self):
        schedule = SessionSchedule(
            calendar=self.calendar, contract=ContractInfo.from_symbol("TMFB6", 2026)
        )
        night = schedule.status(self.local(2, 11, 15))
        morning = schedule.status(self.local(2, 12, 4, 59))
        self.assertEqual(night.phase, "after_hours")
        self.assertEqual(morning.phase, "after_hours")
        self.assertEqual(night.trading_day, "2026-02-23")
        self.assertEqual(morning.trading_day, "2026-02-23")
        self.assertEqual(schedule.status(self.local(2, 12, 10)).phase, "closed")

    def test_expiry_day_1330_close_and_no_afternoon_night_session(self):
        self.assertEqual(self.contract.third_wednesday, date(2026, 10, 21))
        morning = self.schedule.status(self.local(10, 21, 9))
        self.assertEqual(morning.phase, "regular")
        self.assertEqual(morning.reason, "contract_expiry_today")
        self.assertEqual(morning.minutes_until_close, 270)
        self.assertEqual(self.schedule.status(self.local(10, 21, 13, 30)).phase, "closed")
        self.assertEqual(self.schedule.status(self.local(10, 21, 15)).phase, "closed")
        self.assertEqual(self.schedule.status(self.local(10, 22, 2)).phase, "closed")

    def test_engine_disallows_new_entries_on_expiry_day(self):
        config = StrategyConfig(symbol="TMFJ6")
        class AlwaysEnter:
            def decide(self, _quote):
                return StrategyDecision(
                    (OrderIntent("buy", 20000, 1, "entry", "test"),),
                    Features(0, 20000, 0, 0, 0),
                    False, "always enter",
                )
        class ExpiryDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 21, 9, 0, tzinfo=TZ)

        with tempfile.TemporaryDirectory() as folder:
            gateway = ShadowGateway(PositionLedger(config))
            engine = BotEngine(
                config, gateway, Path(folder),
                strategy=AlwaysEnter(), session_schedule=self.schedule,
                risk_state_path=Path(folder) / "risk-state.json",
            )
            example = Quote(
                symbol="TMFJ6", bid=20000, ask=20001,
                bid_qty=1, ask_qty=1, last=20000,
                last_qty=1, total_volume=1,
                exchange_time="09:00:00", received_at=time.monotonic(),
            )
            try:
                with patch("ttb.engine.datetime", ExpiryDatetime):
                    decision = engine.process_quote(example)
                self.assertIsNotNone(decision)
                self.assertEqual(decision.intents, ())
                self.assertEqual(gateway.simulator.orders, {})
            finally:
                engine.recorder.close()
                engine.raw_recorder.close()

    def test_expired_symbol_is_manual_rollover_not_auto_switch(self):
        closed = self.schedule.status(self.local(10, 22, 9))
        self.assertEqual(closed.phase, "closed")
        self.assertEqual(closed.reason, "manual_contract_rollover_required")
        self.assertEqual(self.schedule.contract.symbol, "TMFJ6")

    def test_unknown_next_year_is_fail_closed(self):
        future = SessionSchedule(
            calendar=self.calendar, contract=ContractInfo.from_symbol("TMFA7", 2026)
        )
        result = future.status(self.local(1, 5, 9, year=2027))
        self.assertEqual(result.phase, "closed")
        self.assertEqual(result.reason, "calendar_unverified")

    def test_contract_decoding_and_third_wednesday(self):
        mini = ContractInfo.from_symbol("MTXK6", 2026)
        self.assertEqual((mini.product, mini.month, mini.year), ("MTX", 11, 2026))
        self.assertEqual(mini.third_wednesday, date(2026, 11, 18))
        with self.assertRaises(ValueError):
            ContractInfo.from_symbol("UNRECOGNIZED", 2026)

    def test_next_open_day_skips_holidays_and_weekends(self):
        self.assertEqual(
            self.calendar.next_trading_day(date(2026, 10, 8)), date(2026, 10, 12)
        )
        self.assertEqual(
            self.calendar.next_trading_day(date(2026, 2, 11)), date(2026, 2, 23)
        )
        with self.assertRaises(CalendarCoverageError):
            self.calendar.next_trading_day(date(2026, 12, 31))


if __name__ == "__main__":
    unittest.main()
