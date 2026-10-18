from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sim_hft_bot
from auto_recover_ttb import main as auto_recover_main
from automation.ttb_login import (
    Credential,
    LoginError,
    choose_competition,
    decode_secret,
    ensure_logged_in,
)
from ttb.auto_recovery import (
    AttemptBudget,
    RecoveryDeferred,
    RecoveryPolicy,
    RecoveryRefused,
    auto_recover,
)
from ttb.calendar import CalendarCoverageError, TradingCalendar, front_month_contract, resolve_symbol
from ttb.risk import PreTradeLimits, parse_available_margin
from ttb.session import probe_session

HERE = Path(__file__).parent
CALENDAR = TradingCalendar.from_file(HERE / "automation" / "taifex_calendar_2026.json")
FAST = RecoveryPolicy(settle_seconds=0, poll_seconds=0, timeout_seconds=0.05)


class FrontMonthTests(unittest.TestCase):
    def test_rolls_on_last_trading_day(self):
        self.assertEqual(front_month_contract("TMF", date(2026, 10, 20), CALENDAR).symbol, "TMFJ6")
        self.assertEqual(front_month_contract("TMF", date(2026, 10, 21), CALENDAR).symbol, "TMFK6")
        self.assertEqual(front_month_contract("TXF", date(2026, 11, 19), CALENDAR).symbol, "TXFL6")

    def test_auto_spec_and_explicit_codes(self):
        self.assertEqual(resolve_symbol("auto:mtx", date(2026, 10, 18), CALENDAR), "MTXJ6")
        self.assertEqual(resolve_symbol("txfk6", date(2026, 10, 18), None), "TXFK6")
        with self.assertRaises(CalendarCoverageError):
            resolve_symbol("auto:TMF", date(2026, 10, 18), None)
        with self.assertRaises(ValueError):
            resolve_symbol("auto:TXO", date(2026, 10, 18), CALENDAR)

    def test_unverified_next_year_fails_closed(self):
        with self.assertRaises(CalendarCoverageError):
            front_month_contract("TMF", date(2026, 12, 16), CALENDAR)

    def test_roll_waits_until_flat(self):
        ledger = SimpleNamespace(position=0)
        gateway = SimpleNamespace(active={}, pending={})
        with patch.object(sim_hft_bot, "_resolve_front_month", return_value="TMFK6"):
            self.assertTrue(sim_hft_bot.roll_due("auto:TMF", "TMFJ6", Path("x"), ledger, gateway))
            self.assertFalse(sim_hft_bot.roll_due("auto:TMF", "TMFK6", Path("x"), ledger, gateway))
            ledger.position = 1
            self.assertFalse(sim_hft_bot.roll_due("auto:TMF", "TMFJ6", Path("x"), ledger, gateway))
            ledger.position = 0
            gateway.active = {"buy": {"order_no": "A1"}}
            self.assertFalse(sim_hft_bot.roll_due("auto:TMF", "TMFJ6", Path("x"), ledger, gateway))
        with patch.object(sim_hft_bot, "_resolve_front_month", side_effect=CalendarCoverageError("x")):
            self.assertFalse(sim_hft_bot.roll_due("auto:TMF", "TMFJ6", Path("x"), ledger, gateway))


class SessionProbeTests(unittest.TestCase):
    def api(self, margin_code="0000"):
        return SimpleNamespace(
            query_margin=lambda: {"Code": margin_code, "Data": json.dumps({"AVAILABELMARGIN": "987654.3", "BALN": "1"})},
            query_orders=lambda: {"Code": "0000", "Data": "[]"},
            query_positions=lambda: {"Code": "0000", "Data": "[]"},
        )

    def test_logged_in_reports_field_names_but_no_values(self):
        probe = probe_session(self.api())
        self.assertTrue(probe.logged_in)
        self.assertTrue(probe.documented_margin_field_present)
        self.assertNotIn("987654", json.dumps(probe.as_dict()))

    def test_not_logged_in_or_unreachable(self):
        self.assertFalse(probe_session(self.api("9999")).logged_in)

        def boom():
            raise ConnectionError("down")
        broken = probe_session(SimpleNamespace(query_margin=boom))
        self.assertFalse(broken.api_reachable)
        self.assertEqual(broken.error, "ConnectionError")

    def test_documented_margin_fixture_parses(self):
        response = {"Code": "0000", "Data": json.dumps({"AVAILABELMARGIN": "120,000", "BALN": "500000"})}
        self.assertEqual(parse_available_margin(response, "AVAILABELMARGIN"), 120000.0)


class FakeWindow:
    def __init__(self, items=("115 校內期貨競賽",), logged=False, login_works=True):
        self.items = list(items)
        self.logged = logged
        self.login_works = login_works
        self.selected = ""
        self.filled = None
        self.clicked = False

    def enabled(self, auto_id):
        if auto_id in ("btnLogOut", "cbGameList"):
            return self.logged
        return True

    def enforce_settings(self):
        return ["chkAutoPut=off"]

    def fill_login(self, credential):
        self.filled = credential

    def control(self, auto_id, **_):
        window = self

        class Button:
            def invoke(self):
                window.clicked = True
                window.logged = window.login_works
        return Button()

    def competitions(self):
        return self.items

    def selected_competition(self):
        return self.selected

    def select_competition(self, name):
        self.selected = name

    def subscribe_quote(self, symbol):
        if symbol == "BAD":
            raise RuntimeError("grid changed")
        return "subscribed"

    def dialogs(self):
        return ["帳號或密碼錯誤"]

    def dismiss_dialogs(self):
        pass


def probe_after(window, target):
    from ttb.session import SessionProbe

    def probe():
        result = SessionProbe(api_reachable=True)
        ok = window is not None and window.selected == target
        result.codes = {"margin": "0000" if ok else "9999", "orders": "0000", "positions": "0000"}
        return result
    return probe


class LoginTests(unittest.TestCase):
    def test_choose_competition(self):
        self.assertEqual(choose_competition(["A 競賽"], ""), "A 競賽")
        self.assertEqual(choose_competition(["校內期貨", "全國盃"], "校內"), "校內期貨")
        with self.assertRaises(LoginError):
            choose_competition(["校內 A", "校內 B"], "校內")
        with self.assertRaises(LoginError):
            choose_competition(["A", "B"], "")
        with self.assertRaises(LoginError):
            choose_competition([], "A")

    def test_secret_decoding_and_repr(self):
        self.assertEqual(decode_secret("pässw0rd".encode("utf-16-le")), "pässw0rd")
        self.assertEqual(decode_secret(b"abc"), "abc")
        self.assertNotIn("hunter2", repr(Credential("me@example.com", "hunter2")))

    def test_already_logged_in_never_touches_gui(self):
        from ttb.session import SessionProbe
        ok = SessionProbe(api_reachable=True, codes={"margin": "0000", "orders": "0000", "positions": "0000"})
        result = ensure_logged_in(
            competition="", credential_target="t", ttb_exe=Path("TTB.exe"), probe=lambda: ok,
            window_factory=lambda: self.fail("GUI must not be opened"),
        )
        self.assertEqual(result["action"], "already_logged_in")

    def test_logged_in_session_survives_broken_gui_tooling(self):
        from ttb.session import SessionProbe
        ok = SessionProbe(api_reachable=True, codes={"margin": "0000", "orders": "0000", "positions": "0000"})

        def broken():
            raise ImportError("DLL load failed while importing win32ui")
        result = ensure_logged_in(
            competition="", credential_target="t", ttb_exe=Path("TTB.exe"), probe=lambda: ok,
            window_factory=broken, quote_symbols=("TMFJ6",),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["quotes"], {"TMFJ6": "error:ImportError"})

    def test_full_login_selects_competition_and_verifies(self):
        window = FakeWindow()
        result = ensure_logged_in(
            competition="校內", credential_target="t", ttb_exe=Path("TTB.exe"),
            probe=probe_after(window, "115 校內期貨競賽"), window_factory=lambda: window,
            credential_reader=lambda target: Credential("me@example.com", "pw"),
            login_timeout=0.05, verify_timeout=0.05,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(window.clicked)
        self.assertEqual(window.selected, "115 校內期貨競賽")
        self.assertIn("chkAutoPut=off", result["steps"])

    def test_failed_login_reports_dialog(self):
        window = FakeWindow(login_works=False)
        with self.assertRaises(LoginError) as caught:
            ensure_logged_in(
                competition="", credential_target="t", ttb_exe=Path("TTB.exe"),
                probe=probe_after(window, "x"), window_factory=lambda: window,
                credential_reader=lambda target: Credential("me@example.com", "pw"),
                login_timeout=0.01, verify_timeout=0.01,
            )
        self.assertIn("帳號或密碼錯誤", str(caught.exception))
        self.assertNotIn("pw'", str(caught.exception))

    def test_quote_subscription_is_best_effort(self):
        window = FakeWindow()
        result = ensure_logged_in(
            competition="", credential_target="t", ttb_exe=Path("TTB.exe"),
            probe=probe_after(window, "115 校內期貨競賽"), window_factory=lambda: window,
            credential_reader=lambda target: Credential("me@example.com", "pw"),
            login_timeout=0.05, verify_timeout=0.05, quote_symbols=("TMFK6", "BAD"),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["quotes"], {"TMFK6": "subscribed", "BAD": "error:RuntimeError"})

    def test_no_submit_stops_before_login(self):
        window = FakeWindow()
        result = ensure_logged_in(
            competition="", credential_target="t", ttb_exe=Path("TTB.exe"),
            probe=probe_after(None, "x"), window_factory=lambda: window,
            credential_reader=lambda target: Credential("me@example.com", "pw"), submit=False,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(window.clicked)


class FakeHttp:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return SimpleNamespace(text=self.replies.pop(0), status_code=200)


class HttpModuleTests(unittest.TestCase):
    def test_handshake_rejection_raises_instead_of_exiting(self):
        from ttb.api import HttpTTBModule, TTBApiError
        module = HttpTTBModule("http://localhost:8080/", session=FakeHttp(['{"Ver":"x","ErrMsg":"版本不符"}']))
        with self.assertRaises(TTBApiError):
            module.handshake("2025.11.27.1900")

    def test_endpoints_match_ttbhelp_and_use_timeouts(self):
        from ttb.api import HttpTTBModule, TTBApi, TTBApiError
        http = FakeHttp(['{"Ver":"v","ErrMsg":""}', '{"Code":"0000","Data":"[]"}', 'not json'])
        module = HttpTTBModule("http://localhost:8080", timeout=3, session=http)
        module.handshake("v")
        self.assertEqual(TTBApi(module).query_orders()["Code"], "0000")
        with self.assertRaises(TTBApiError):
            module.QUERYMARGIN()
        self.assertEqual([call[0].rsplit("/", 1)[1] for call in http.calls],
                         ["CONNECT", "QUERYRESTOREREPORT", "QUERYMARGIN"])
        self.assertTrue(all(call[2] == 3 for call in http.calls))
        self.assertEqual(module.QUOTEDATA("TMFK6")["Code"], "000")


class FakeBroker:
    """Mutable TTB account: cancels and market IOC orders take effect."""

    def __init__(self, orders=(), positions=(), fills_market=True, code="0000"):
        self.orders = [dict(row) for row in orders]
        self.positions = [dict(row) for row in positions]
        self.fills_market = fills_market
        self.code = code
        self.cancelled = []
        self.market_orders = []

    def query_orders(self):
        return {"Code": self.code, "Data": json.dumps(self.orders)}

    def query_positions(self):
        return {"Code": self.code, "Data": json.dumps(self.positions)}

    def query_margin(self):
        return {"Code": self.code, "Data": "{}"}

    def cancel_order(self, order_no):
        self.cancelled.append(order_no)
        for row in self.orders:
            if row["ORDNO"] == order_no:
                row.update(LESS_VOLM="0", STAT="已取消")

    def new_market_ioc(self, symbol, side, quantity):
        self.market_orders.append((symbol, side, quantity))
        if self.fills_market:
            self.positions = [row for row in self.positions if row["ComdID1"] != symbol]
        return {"Code": "0000"}


def working(order_no="W1", symbol="TMFJ6"):
    return {"ORDNO": order_no, "COMD_ID": symbol, "VOLM": "1", "LESS_VOLM": "1", "DEAL_TOTL": "0", "STAT": "委託成功"}


def long_position(symbol="TMFJ6", qty="1", side="B"):
    return {"ComdID1": symbol, "ComdBS1": side, "Hold": qty}


class AutoRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.flag = root / "manual-reconciliation-required.flag"
        self.flag.write_text("x", encoding="ascii")
        self.audit = root / "audit"
        self.budget = AttemptBudget(root / "budget.json", 2)

    def tearDown(self):
        self.tmp.cleanup()

    def run_recovery(self, broker, market_open=True):
        return auto_recover(
            broker, flag=self.flag, audit_dir=self.audit, budget=self.budget,
            policy=FAST, market_open=market_open, sleep=lambda _: None, today="2026-10-19",
        )

    def test_clean_account_clears_flag_without_spending_budget(self):
        result = self.run_recovery(FakeBroker())
        self.assertEqual(result["action"], "already_clean")
        self.assertFalse(self.flag.exists())
        self.assertFalse((Path(self.tmp.name) / "budget.json").exists())

    def test_cancels_and_flattens_then_clears(self):
        broker = FakeBroker([working()], [long_position(qty="2", side="S")])
        result = self.run_recovery(broker)
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(broker.cancelled, ["W1"])
        self.assertEqual(broker.market_orders, [("TMFJ6", "buy", 2)])
        self.assertFalse(self.flag.exists())
        self.assertTrue(any("auto_recovery_clear" in p.read_text(encoding="utf-8") for p in self.audit.iterdir()))

    def test_position_waits_for_open_market(self):
        broker = FakeBroker(positions=[long_position()])
        with self.assertRaises(RecoveryDeferred):
            self.run_recovery(broker, market_open=False)
        self.assertEqual(broker.market_orders, [])
        self.assertTrue(self.flag.exists())

    def test_logged_out_reports_are_deferred(self):
        with self.assertRaises(RecoveryDeferred):
            self.run_recovery(FakeBroker(code="9999"))
        self.assertTrue(self.flag.exists())

    def test_refuses_non_futures_and_oversized_positions(self):
        with self.assertRaises(RecoveryRefused):
            self.run_recovery(FakeBroker(positions=[long_position(symbol="TXO27000K6")]))
        with self.assertRaises(RecoveryRefused):
            self.run_recovery(FakeBroker(positions=[long_position(qty="50")]))
        self.assertTrue(self.flag.exists())

    def test_unfilled_flatten_keeps_latch(self):
        broker = FakeBroker(positions=[long_position()], fills_market=False)
        with self.assertRaises(RecoveryRefused):
            self.run_recovery(broker)
        self.assertTrue(self.flag.exists())

    def test_daily_budget_is_durable(self):
        for _ in range(2):
            self.run_recovery(FakeBroker([working()]))
            self.flag.write_text("x", encoding="ascii")
        with self.assertRaises(RecoveryRefused):
            self.run_recovery(FakeBroker([working()]))
        self.assertEqual(self.budget.consume("2026-10-20"), 1)

    def test_cli_requires_exclusive_account_confirmation(self):
        with patch("ttb.api.TTBApi.connect", side_effect=AssertionError("must not connect")):
            self.assertEqual(auto_recover_main([]), 2)


class CompetitionConfigTests(unittest.TestCase):
    def test_competition_example_passes_live_gates(self):
        config = json.loads((HERE / "automation" / "config.competition.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["mode"], "ttb")
        self.assertTrue(config["autoRecovery"]["exclusiveBotAccount"])
        args = sim_hft_bot.parse_args(["--symbol", "TMFJ6"] + config["extraArgs"])
        limits = PreTradeLimits(
            max_notional=args.max_notional,
            minimum_available_margin=args.min_available_margin,
            available_margin_field=args.available_margin_field,
        )
        limits.validate()
        self.assertEqual(args.available_margin_field, "AVAILABELMARGIN")

    def test_supervisor_and_installer_guards(self):
        supervisor = (HERE / "automation" / "run_ttb_automation.ps1").read_text(encoding="utf-8")
        self.assertIn("Test-AutoRecoveryAllowed", supervisor)
        self.assertIn("exclusiveBotAccount", supervisor)
        self.assertIn("--confirm-exclusive-bot-account", supervisor)
        # TTB opens its API ports only after login: login must come first.
        self.assertLess(supervisor.index("(Invoke-TtbLogin) -ne 0"), supervisor.index("Test-LocalPort 8080) -or"))
        self.assertIn("'--stop-file', $botStopFile", supervisor)
        self.assertIn("TTB process exited", supervisor)
        installer = (HERE / "automation" / "install_azure_tasks.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Register-ScheduledTask -TaskName 'TTB GUI'", installer)


if __name__ == "__main__":
    unittest.main()
