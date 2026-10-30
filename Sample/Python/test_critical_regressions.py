from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sim_hft_bot import build_config, parse_args
from ttb.contracts import point_value_for
from ttb.orders import _response_rows, order_remaining, reported_position
from ttb.runtime import RiskStateStore, SessionSchedule


class CriticalRegressionTests(unittest.TestCase):
    def test_contract_multiplier_auto_detected(self):
        for symbol, expected in (
            ("TMFJ6", 10), ("MTXJ6", 50), ("TXFJ6", 200),
            ("tmfj6", 10),
        ):
            with self.subTest(symbol=symbol):
                args = parse_args(["--symbol", symbol])
                self.assertEqual(build_config(args).point_value, expected)

    def test_wrong_multiplier_cannot_override_known_contract(self):
        with self.assertRaisesRegex(ValueError, "contract multiplier"):
            point_value_for("TMFJ6", 200)
        with self.assertRaisesRegex(ValueError, "unknown contract"):
            point_value_for("UNKNOWN")
        self.assertEqual(point_value_for("UNKNOWN", 7), 7)

    def test_missing_or_malformed_broker_data_is_never_flat(self):
        for raw in (
            {"Code": "0000"},
            {"Code": "0000", "Data": ""},
            {"Code": "0000", "Data": "null"},
            {"Code": "0000", "Data": [{"Hold": "0"}, "bad row"]},
        ):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                _response_rows(raw)
        with self.assertRaisesRegex(RuntimeError, "missing Data"):
            reported_position({"Code": "0000"}, "TXFA7")

    def test_unknown_hold_and_symbol_cannot_be_parsed_as_empty_account(self):
        with self.assertRaisesRegex(RuntimeError, "missing Hold"):
            reported_position({"Code": "0000", "Data": [{"ComdID1": "TXFA7"}]}, "TXFA7")
        with self.assertRaisesRegex(RuntimeError, "no symbol"):
            reported_position({"Code": "0000", "Data": [{"Hold": "2"}]}, "TXFA7")
        with self.assertRaisesRegex(RuntimeError, "unknown side"):
            reported_position({"Code": "0000", "Data": [{"Hold": "2", "ComdID1": "TXFA7"}]}, "TXFA7")

    def test_missing_order_quantity_does_not_become_zero(self):
        with self.assertRaisesRegex(RuntimeError, "remaining quantity"):
            order_remaining({"ORDNO": "A1", "STAT": "委託成功"})
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            order_remaining({"ORDNO": "A1", "LESS_VOLM": "-1", "STAT": "委託成功"})

    def test_friday_and_saturday_night_share_risk_trading_day(self):
        schedule = SessionSchedule()
        tz = timezone(timedelta(hours=8))
        friday = datetime(2026, 10, 16, 23, 59, tzinfo=tz)
        saturday = datetime(2026, 10, 17, 0, 1, tzinfo=tz)
        self.assertEqual(schedule.status(friday).trading_day, "2026-10-19")
        self.assertEqual(schedule.status(saturday).trading_day, "2026-10-19")
        self.assertEqual(schedule.status(friday).phase, "after_hours")
        self.assertEqual(schedule.status(saturday).phase, "after_hours")

    def test_corrupt_risk_state_never_silently_resets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "risk-state.json"
            risk = RiskStateStore(path, "TXFA7")
            self.assertEqual(risk.load("2026-10-13"), {})
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                risk.load("2026-10-13")
            path.write_text(json.dumps({"version": 1, "symbol": "TXFA7"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "trading day"):
                risk.load("2026-10-13")


if __name__ == "__main__":
    unittest.main()
