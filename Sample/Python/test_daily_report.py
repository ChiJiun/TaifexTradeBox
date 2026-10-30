import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from automation import notify
from automation.daily_report import TAIPEI, Book, Costs, Fill, build_report, intraday_curve, report_message
from automation.notify import post_files, run, twd_margin
from test_notify import CALENDAR, MARGIN, fake_webhook
from ttb.journal import ExecutionJournal
from ttb.trade_db import sync_journal


def at(hour, minute, second=0, day=30):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=TAIPEI)


def tmf(side, price, quantity, when):
    return Fill("TMFK6", when.date().isoformat(), when, side, price, quantity, "")


class BookTests(unittest.TestCase):
    def test_fills_pair_first_in_first_out_and_carry_the_rest(self):
        book = Book(Costs(fee_per_fill=50, tax_rate=0))
        book.apply(tmf("buy", 100, 1, at(9, 0)))
        book.apply(tmf("buy", 102, 1, at(9, 1)))
        book.apply(tmf("sell", 104, 3, at(9, 2)))
        first, second = book.closed
        self.assertEqual((first.entry, first.exit, first.direction), (100, 104, 1))
        self.assertEqual((second.entry, second.exit), (102, 104))
        # TMF is 10 NTD a point; each lot pays the fee on both fills.
        self.assertEqual((first.gross, first.net), (40, -60))
        self.assertEqual(book.position("TMFK6"), -1)
        # The open short is marked net of its own entry fee.
        self.assertEqual(book.unrealized({"TMFK6": 103}), 10 - 50)

    def test_intraday_curve_starts_from_zero_each_day_and_marks_open_lots(self):
        costs = Costs(fee_per_fill=0, tax_rate=0)
        fills = [tmf("buy", 100, 1, at(9, 0, day=29)), tmf("sell", 110, 1, at(9, 5, day=29)),
                 tmf("sell", 120, 1, at(9, 0)), tmf("buy", 118, 1, at(9, 10))]
        prices = {"TMFK6": [(at(9, 5), 125.0), (at(9, 20), 117.0)]}
        curve = intraday_curve(fills, prices, "2026-10-30", costs)
        self.assertEqual([value for _, value in curve], [0, -50, 20, 20])


class BuildReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        journal = ExecutionJournal(self.root / "journal", "TMFK6")
        for day, order_id, side, price in [("2026-10-29", "A", "buy", 100.0), ("2026-10-29", "B", "sell", 90.0),
                                           ("2026-10-30", "C", "sell", 120.0), ("2026-10-30", "D", "buy", 110.0),
                                           ("2026-10-30", "E", "buy", 111.0)]:
            journal.append("fill", {"order_id": order_id, "side": side, "price": price,
                                    "quantity": 1, "deal_total": 1}, day)
        self.db = self.root / "trades.sqlite3"
        sync_journal(self.root / "journal", self.db)
        runs = self.root / "runs" / "TMFK6"
        runs.mkdir(parents=True)
        with (runs / "ttb-shadow-20261030-090000.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["wall_time", "symbol", "last"])
            writer.writerow(["2026-10-30 09:00:00", "TMFK6", "119"])
            writer.writerow(["2026-10-30 13:40:00", "TMFK6", "112"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_chart_and_review_csvs_for_the_day(self):
        report = build_report(day="2026-10-30", db_path=self.db, runs_dir=self.root / "runs",
                              out_dir=self.root / "reports", costs=Costs(fee_per_fill=0, tax_rate=0))
        paths = report["paths"]
        self.assertEqual(paths["chart"].read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        with paths["fills"].open(encoding="utf-8-sig") as stream:
            fills = list(csv.DictReader(stream))
        self.assertEqual([(row["買賣"], row["價格"], row["成交後部位"], row["委託書號"]) for row in fills],
                         [("賣", "120.0", "-1", "C"), ("買", "110.0", "0", "D"), ("買", "111.0", "1", "E")])
        with paths["roundtrips"].open(encoding="utf-8-sig") as stream:
            trips = list(csv.DictReader(stream))
        self.assertEqual([(row["方向"], row["進場價"], row["出場價"], row["淨損益"]) for row in trips],
                         [("空", "120.0", "110.0", "100")])
        # 10-29 lost 100, 10-30 closed +100: back to flat since the start.
        self.assertEqual((report["round_trips"], report["wins"], report["net"], report["cumulative"]),
                         (1, 1, 100, 0))
        self.assertIn("沖銷 1 筆（勝率 100%）", report_message("2026-10-30", report))


class ReportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.journal_dir, self.db, self.state = root / "journal", root / "trades.sqlite3", root / "state.json"
        ExecutionJournal(self.journal_dir, "TMFK6")
        self.reports = []

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, hour, minute, report=None):
        return run(now=datetime(2026, 10, 29, hour, minute, tzinfo=TAIPEI), url="https://hook",
                   db_path=self.db, journal_dir=self.journal_dir, state_path=self.state, calendar=CALENDAR,
                   snapshot=lambda: (twd_margin(MARGIN), 0), send=lambda *args, **kwargs: None,
                   report=report or self.reports.append)

    def test_report_follows_the_summary_once_a_day(self):
        self.assertEqual(self.tick(13, 40), [])
        self.assertEqual(self.tick(13, 47), ["summary", "report"])
        self.assertEqual(self.reports, ["2026-10-29"])
        self.assertEqual(self.tick(13, 48), [])

    def test_a_failing_report_is_retried_a_few_times_then_dropped(self):
        def broken(_day):
            raise RuntimeError("no matplotlib")
        with self.assertRaises(RuntimeError):
            self.tick(13, 47, broken)  # the summary still went out first
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["summary_day"], "2026-10-29")
        for minute in (48, 49):
            with self.assertRaises(RuntimeError):
                self.tick(13, minute, broken)
        self.assertEqual(self.tick(13, 50, broken), [])

    def test_files_are_posted_as_a_discord_multipart_message(self):
        chart = Path(self.tmp.name) / "report.png"
        chart.write_bytes(b"\x89PNG")
        with fake_webhook() as (url, bodies):
            post_files(url, "收盤圖表", [chart])
        content_type, body = bodies[0]
        self.assertTrue(content_type.startswith("multipart/form-data"))
        self.assertIn(b'name="files[0]"; filename="report.png"', body)
        self.assertIn(json.dumps({"content": "收盤圖表"}).encode("ascii"), body)

    def test_notifier_main_wires_the_report_to_the_same_webhook(self):
        with patch.object(notify, "run", return_value=[]) as fake_run, \
                patch.dict("os.environ", {"TTB_ALERT_WEBHOOK_URL": "https://hook"}):
            notify.main(["--db", str(self.db), "--state", str(self.state), "--journal-dir", str(self.journal_dir)])
        with patch.object(notify, "daily_report") as daily:
            fake_run.call_args.kwargs["report"]("2026-10-29")
        daily.assert_called_once_with("2026-10-29", self.db, "https://hook")


if __name__ == "__main__":
    unittest.main()
