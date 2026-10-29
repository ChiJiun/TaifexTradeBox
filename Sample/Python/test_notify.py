import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from automation.notify import TAIPEI, run, summary_message, twd_margin
from ttb.calendar import TradingCalendar
from ttb.journal import ExecutionJournal

HERE = Path(__file__).resolve().parent
CALENDAR = TradingCalendar.from_file(HERE / "automation" / "taifex_calendar_2026.json")
MARGIN = {"Code": "0000", "Data": [
    {"CURRENCY": "TWD", "PREV_BALN": "2000000", "ACCT_NETV_FUTS": "2001250", "TRAN_FEE": "60", "TRAN_TAX": "40"},
    {"CURRENCY": "USD", "PREV_BALN": "0", "ACCT_NETV_FUTS": "0"},
]}


def fill(journal, order_id, side, price, day="2026-10-29"):
    journal.append("fill", {"order_id": order_id, "side": side, "price": price,
                            "quantity": 1, "deal_total": 1}, day)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.journal_dir, self.db, self.state = root / "journal", root / "trades.sqlite3", root / "state.json"
        self.journal = ExecutionJournal(self.journal_dir, "TMFK6")
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, hour, minute, snapshot=None, day=29):
        def send(url, text):
            self.sent.append(text)
        return run(now=datetime(2026, 10, day, hour, minute, tzinfo=TAIPEI), url="https://hook",
                   db_path=self.db, journal_dir=self.journal_dir, state_path=self.state,
                   calendar=CALENDAR, snapshot=snapshot or (lambda: (twd_margin(MARGIN), 0)), send=send)

    def test_reports_each_new_fill_once_and_skips_history_on_first_run(self):
        fill(self.journal, "Old1", "buy", 46700.0)
        self.assertEqual(self.tick(9, 30), [])
        fill(self.journal, "Dm1", "buy", 46712.0)
        fill(self.journal, "Dm2", "sell", 46715.0)
        self.assertEqual(self.tick(9, 31), ["fills:2"])
        self.assertIn("買 1 口 @ 46712.0", self.sent[0])
        self.assertIn("賣 1 口 @ 46715.0", self.sent[0])
        self.assertNotIn("Old1", self.sent[0])
        self.assertEqual(self.tick(9, 32), [])

    def test_failed_send_is_retried_next_minute(self):
        self.tick(9, 30)
        fill(self.journal, "Dm1", "buy", 46712.0)
        with self.assertRaises(RuntimeError):
            run(now=datetime(2026, 10, 29, 9, 31, tzinfo=TAIPEI), url="https://hook", db_path=self.db,
                journal_dir=self.journal_dir, state_path=self.state, calendar=CALENDAR,
                snapshot=lambda: (twd_margin(MARGIN), 0),
                send=lambda url, text: (_ for _ in ()).throw(RuntimeError("discord down")))
        self.assertEqual(self.tick(9, 32), ["fills:1"])

    def test_daily_summary_once_after_the_close_on_trading_days_only(self):
        self.tick(9, 30)
        fill(self.journal, "Dm1", "buy", 46712.0)
        fill(self.journal, "Dm2", "sell", 46715.0)
        self.assertEqual(self.tick(13, 40), ["fills:2"])
        self.assertEqual(self.tick(13, 47), ["summary"])
        summary = self.sent[-1]
        self.assertIn("成交 2 筆（買 1 口、賣 1 口）", summary)
        self.assertIn("當日損益 +1,250", summary)
        self.assertIn("收盤部位 0", summary)
        self.assertEqual(self.tick(13, 50), [])
        self.assertEqual(self.tick(13, 50, day=31), [])  # Saturday

    def test_summary_still_sent_when_ttb_is_unreachable(self):
        self.tick(9, 30)

        def broken():
            raise ConnectionError("TTB closed")
        self.assertEqual(self.tick(13, 47, snapshot=broken), ["summary"])
        self.assertIn("無法向 TTB 取得帳務：ConnectionError: TTB closed", self.sent[-1])

    def test_summary_shows_an_open_position(self):
        text = summary_message("2026-10-29", [], twd_margin(MARGIN), -2)
        self.assertIn("收盤部位 -2 口", text)


class WatchdogMarketHoursTests(unittest.TestCase):
    def test_watchdog_only_alerts_inside_the_session(self):
        script = (HERE / "automation" / "health_watchdog.ps1").read_text(encoding="utf-8")
        body = script[script.index("function Test-MarketHours"):script.index("$alerted = ")]
        cases = {
            "2026-10-29T08:50:00+08:00": False, "2026-10-29T09:30:00+08:00": True,
            "2026-10-29T13:41:00+08:00": False, "2026-10-31T10:00:00+08:00": False,  # Saturday
            "2026-12-25T10:00:00+08:00": False,  # exchange holiday
        }
        command = (
            f"$WorkDir = '{HERE}'; $AlertFromMinutes = 535; $AlertUntilMinutes = 820\n{body}\n"
            + "\n".join(f"Test-MarketHours ([datetimeoffset]::Parse('{at}').UtcDateTime)" for at in cases)
        )
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(result.stdout.split(), [str(v) for v in cases.values()])


if __name__ == "__main__":
    unittest.main()
