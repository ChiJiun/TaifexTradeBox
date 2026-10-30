import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from automation.notify import TAIPEI, mention_user_id, post, run, summary_message, twd_margin
from ttb.calendar import TradingCalendar
from ttb.journal import ExecutionJournal

HERE = Path(__file__).resolve().parent
CALENDAR = TradingCalendar.from_file(HERE / "automation" / "taifex_calendar_2026.json")
MARGIN = {"Code": "0000", "Data": [
    {"CURRENCY": "TWD", "PREV_BALN": "2000000", "ACCT_NETV_FUTS": "2001250", "TRAN_FEE": "60", "TRAN_TAX": "40"},
    {"CURRENCY": "USD", "PREV_BALN": "0", "ACCT_NETV_FUTS": "0"},
]}


MENTION = "123456789012345678"


@contextmanager
def fake_webhook():
    """A local stand-in for the Discord webhook; yields (url, bodies)."""
    bodies = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append((self.headers.get("Content-Type"), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", bodies
    finally:
        server.shutdown()
        server.server_close()


def fill(journal, order_id, side, price, day="2026-10-29"):
    journal.append("fill", {"order_id": order_id, "side": side, "price": price,
                            "quantity": 1, "deal_total": 1}, day)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.journal_dir, self.db, self.state = root / "journal", root / "trades.sqlite3", root / "state.json"
        self.journal = ExecutionJournal(self.journal_dir, "TMFK6")
        self.sent, self.mentions = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, hour, minute, snapshot=None, day=29, mention=""):
        def send(url, text, mention_user_id=""):
            self.sent.append(text)
            self.mentions.append(mention_user_id)
        return run(now=datetime(2026, 10, day, hour, minute, tzinfo=TAIPEI), url="https://hook",
                   db_path=self.db, journal_dir=self.journal_dir, state_path=self.state,
                   calendar=CALENDAR, snapshot=snapshot or (lambda: (twd_margin(MARGIN), 0)), send=send,
                   mention_user_id=mention)

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

    def test_summary_is_titled_daily_settlement(self):
        text = summary_message("2026-10-29", [], twd_margin(MARGIN), 0)
        self.assertTrue(text.startswith("📊 **【每日結算】2026-10-29**"), text)

    def test_only_the_daily_summary_mentions_the_user(self):
        self.tick(9, 30, mention=MENTION)
        fill(self.journal, "Dm1", "buy", 46712.0)
        self.assertEqual(self.tick(9, 31, mention=MENTION), ["fills:1"])
        self.assertEqual(self.tick(13, 47, mention=MENTION), ["summary"])
        self.assertEqual(self.mentions, ["", MENTION])


class MentionPayloadTests(unittest.TestCase):
    def posted(self, mention):
        with fake_webhook() as (url, bodies):
            post(url, "📊 **【每日結算】2026-10-29**", mention)
        self.assertEqual(len(bodies), 1)
        return json.loads(bodies[0][1].decode("utf-8"))

    def test_mention_pings_only_that_user(self):
        payload = self.posted(MENTION)
        self.assertEqual(payload["content"], f"<@{MENTION}> 📊 **【每日結算】2026-10-29**")
        self.assertEqual(payload["allowed_mentions"], {"users": [MENTION]})
        self.assertEqual(payload["text"], "📊 **【每日結算】2026-10-29**")  # Slack gets no Discord tag

    def test_without_a_mention_the_payload_is_unchanged(self):
        payload = self.posted("")
        self.assertEqual(payload, {"content": "📊 **【每日結算】2026-10-29**", "text": "📊 **【每日結算】2026-10-29**"})

    def test_only_a_numeric_id_counts_as_a_mention(self):
        self.assertEqual(mention_user_id(f"  {MENTION}\n"), MENTION)
        for raw in ("", "   ", "<@123>", "@everyone", "１２３", "12a3"):
            self.assertEqual(mention_user_id(raw), "", raw)


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



class WatchdogRetryTests(unittest.TestCase):
    def run_watchdog(self, failures):
        with tempfile.TemporaryDirectory() as work:
            check = Path(work) / "automation" / "check_ttb_health.ps1"
            check.parent.mkdir()
            # Fails the first `failures` calls, then reports healthy.
            check.write_text(
                "$n = Join-Path $PSScriptRoot 'calls.txt'\n"
                "$c = 1 + [int](Get-Content $n -ErrorAction SilentlyContinue)\n"
                f"Set-Content $n $c\nif ($c -le {failures}) {{ exit 2 }}\nexit 0\n",
                encoding="utf-8")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(HERE / "automation" / "health_watchdog.ps1"), "-WorkDir", work,
                 "-AlertWebhookUrl", "", "-RetryDelaySeconds", "0"],
                capture_output=True, text=True, timeout=120)
            calls = int((check.parent / "calls.txt").read_text().strip())
            return result.returncode, calls

    def test_a_transient_failure_is_retried_before_it_counts(self):
        self.assertEqual(self.run_watchdog(failures=1), (0, 2))

    def test_a_persistent_failure_still_fails_after_every_attempt(self):
        self.assertEqual(self.run_watchdog(failures=99), (2, 3))


class WatchdogMessageTests(unittest.TestCase):
    def recover(self, *extra_args, env=None):
        """Run the watchdog once after a failure alert; return the posted payloads and the error log."""
        with fake_webhook() as (url, bodies), tempfile.TemporaryDirectory() as work:
            check = Path(work) / "automation" / "check_ttb_health.ps1"
            check.parent.mkdir()
            check.write_text("exit 0\n", encoding="utf-8")
            state = Path(work) / "output" / "supervisor" / "health-watchdog-state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"alerted": True, "lastAlertAt": "2026-10-30T09:21:00+08:00"}),
                             encoding="utf-8")
            environ = {k: v for k, v in os.environ.items() if k != "TTB_ALERT_MENTION_USER_ID"}
            environ.update(env or {})
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(HERE / "automation" / "health_watchdog.ps1"), "-WorkDir", work,
                 "-AlertWebhookUrl", url, *extra_args],
                capture_output=True, text=True, timeout=120, check=True, env=environ)
            log = state.parent / "health-watchdog-error.log"
            error_log = log.read_text(encoding="utf-8-sig") if log.exists() else ""
        for content_type, _raw in bodies:
            self.assertIn("charset=utf-8", content_type)
        return [json.loads(raw.decode("utf-8")) for _type, raw in bodies], error_log

    def test_recovery_posts_a_chinese_utf8_message_to_the_webhook(self):
        payloads, _log = self.recover()
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["kind"], "recovery")
        self.assertTrue(payload["content"].startswith("✅【已恢復】TaifexTradeBox bot 健康檢查恢復正常"),
                        payload["content"])
        # Taipei time even on a UTC host (the CI runner).
        self.assertIn("最後一次故障通知於 10/30 09:21", payload["content"])
        self.assertNotIn("allowed_mentions", payload)

    def test_mention_id_from_the_environment_pings_only_that_user(self):
        payloads, _log = self.recover(env={"TTB_ALERT_MENTION_USER_ID": MENTION})
        payload = payloads[0]
        self.assertTrue(payload["content"].startswith(f"<@{MENTION}> ✅【已恢復】"), payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"users": [MENTION]})
        self.assertTrue(payload["text"].startswith("✅【已恢復】"))  # Slack gets no Discord tag

    def test_a_malformed_mention_id_is_logged_and_the_alert_still_goes_out(self):
        payloads, error_log = self.recover("-MentionUserId", "@everyone")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["content"].startswith("✅【已恢復】"), payloads[0]["content"])
        self.assertNotIn("allowed_mentions", payloads[0])
        self.assertIn("TTB_ALERT_MENTION_USER_ID is not a numeric Discord user ID", error_log)

    def summary(self, call):
        script = (HERE / "automation" / "health_watchdog.ps1").read_text(encoding="utf-8-sig")
        body = script[script.index("function Format-AlertSummary"):script.index("$alerted = ")]
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + body + "\n" + call],
                                capture_output=True, timeout=60, check=True)
        return result.stdout.decode("utf-8").strip()

    def test_recovery_time_is_taipei_time_whatever_the_alert_offset(self):
        text = self.summary("Format-AlertSummary 'recovery' 0 'vm' '2026-10-30T01:21:00Z'")
        self.assertIn("最後一次故障通知於 10/30 09:21", text)

    def test_an_unreadable_alert_time_still_gives_the_recovery_message(self):
        text = self.summary("Format-AlertSummary 'recovery' 0 'vm' 'not a time'")
        self.assertTrue(text.startswith("✅【已恢復】"), text)
        self.assertNotIn("最後一次故障通知於", text)

    def test_failure_message_says_it_failed_and_gives_the_exit_code(self):
        text = self.summary("Format-AlertSummary 'failure' 2 'ttb-sim-gcp' $null")
        self.assertTrue(text.startswith("🔴【故障】TaifexTradeBox bot 健康檢查失敗"), text)
        self.assertIn("exit=2", text)
        self.assertIn("ttb-sim-gcp", text)


if __name__ == "__main__":
    unittest.main()
