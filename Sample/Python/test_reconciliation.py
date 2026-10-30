import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reconcile_ttb import _config, parse_args, reconcile
from ttb.reconciliation import (
    AccountInspectionError,
    clear_reconciliation_flag,
    inspect_account,
)


class FakeAccount:
    def __init__(self, orders=None, positions=None, margin=None):
        self.orders = orders if orders is not None else []
        self.positions = positions if positions is not None else []
        self.margin = margin if margin is not None else {"Code": "0000", "Data": []}
        self.calls = 0

    def query_orders(self):
        self.calls += 1
        return {"Code": "0000", "Data": self.orders}

    def query_positions(self):
        return {"Code": "0000", "Data": self.positions}

    def query_margin(self):
        return self.margin


def working(order_no="W1", symbol="TXFA7", qty="1"):
    return {
        "ORDNO": order_no, "COMD_ID": symbol, "VOLM": "1",
        "LESS_VOLM": qty, "DEAL_TOTL": "0", "STAT": "委託成功",
    }


def position(symbol="TXFA7", hold="1"):
    return {"ComdID1": symbol, "ComdBS1": "1", "Hold": hold}


class AccountInspectionTests(unittest.TestCase):
    def test_empty_account_safe_but_api_does_not_verify_gui_identity(self):
        result = inspect_account(FakeAccount(), "TXFA7")
        self.assertTrue(result.can_clear)
        self.assertFalse(result.account_identity_verified_by_api)
        self.assertEqual(result.working_orders, [])
        self.assertEqual(result.open_positions, [])

    def test_entire_account_not_just_managed_symbol_is_checked(self):
        result = inspect_account(
            FakeAccount(orders=[working(symbol="MTXA7")],
                        positions=[position(symbol="TMFA7")]),
            "TXFA7",
        )
        self.assertFalse(result.can_clear)
        self.assertEqual(result.working_orders[0]["ownership"], "unverified")
        self.assertEqual(result.open_positions[0]["symbol"], "TMFA7")

    def test_terminal_historical_order_is_not_active(self):
        api = FakeAccount(orders=[{
            "ORDNO": "OLD", "COMD_ID": "TXFA7", "VOLM": "1",
            "LESS_VOLM": "0", "DEAL_TOTL": "0", "STAT": "取消",
        }])
        self.assertTrue(inspect_account(api, "TXFA7").can_clear)

    def test_missing_data_field_fails_not_empty(self):
        api = FakeAccount()
        api.query_positions = lambda: {"Code": "0000"}
        with self.assertRaisesRegex(AccountInspectionError, "missing Data"):
            inspect_account(api, "TXFA7")

    def test_api_error_fails_closed(self):
        api = FakeAccount()
        api.query_orders = lambda: {"Code": "9999", "Data": []}
        with self.assertRaisesRegex(AccountInspectionError, "success code"):
            inspect_account(api, "TXFA7")

    def test_malformed_position_or_order_fails_closed(self):
        with self.assertRaisesRegex(AccountInspectionError, "malformed quantity"):
            inspect_account(FakeAccount(positions=[position(hold="N/A")]), "TXFA7")
        with self.assertRaisesRegex(AccountInspectionError, "remaining quantity"):
            inspect_account(FakeAccount(orders=[{"ORDNO": "UNKNOWN", "STAT": "委託成功"}]), "TXFA7")
        with self.assertRaisesRegex(AccountInspectionError, "order number"):
            inspect_account(FakeAccount(orders=[{"VOLM": "1", "LESS_VOLM": "1"}]), "TXFA7")

    def test_inconsistent_zero_remaining_without_terminal_status_is_blocked(self):
        with self.assertRaisesRegex(AccountInspectionError, "zero remaining"):
            inspect_account(FakeAccount(orders=[{
                "ORDNO": "A01", "COMD_ID": "TXFA7",
                "LESS_VOLM": "0", "STAT": "委託成功",
            }]), "TXFA7")

    def test_margin_query_failure_is_visible_but_does_not_fake_positions(self):
        api = FakeAccount(margin={"Code": "9999"})
        result = inspect_account(api, "TXFA7")
        self.assertTrue(result.can_clear)
        self.assertFalse(result.margin_query_ok)
        self.assertIn("failed", result.margin_note)


class ReconciliationFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.flag = self.root / "manual-reconciliation-required.flag"
        self.audit = self.root / "reconciliation"

    def test_read_only_writes_audit_without_mutating_latch(self):
        self.flag.write_text("error")
        report = reconcile(
            FakeAccount(), symbol="TXFA7", flag=self.flag, audit_dir=self.audit
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["action"], "read_only")
        self.assertTrue(self.flag.exists())
        saved = json.loads(Path(report["audit_file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["action"], "read_only")
        self.assertNotIn("Data", saved)

    def test_clearing_requires_explicit_confirmations(self):
        self.flag.write_text("error")
        with self.assertRaisesRegex(AccountInspectionError, "confirmations"):
            reconcile(
                FakeAccount(), symbol="TXFA7", flag=self.flag,
                audit_dir=self.audit, clear=True,
            )
        self.assertTrue(self.flag.exists())

    def test_two_verified_flat_reads_and_audit_allow_flag_clear(self):
        self.flag.write_text("error")
        with patch("reconcile_ttb.time.sleep"):
            report = reconcile(
                FakeAccount(), symbol="TXFA7", flag=self.flag, audit_dir=self.audit,
                clear=True, confirmations=(True, True, True, True),
            )
        self.assertEqual(report["action"], "flag_cleared")
        self.assertFalse(self.flag.exists())
        self.assertTrue(Path(report["audit_file"]).is_file())

    def test_second_read_detects_intervening_open_position(self):
        self.flag.write_text("error")
        class ChangingAccount(FakeAccount):
            def query_positions(self):
                return {"Code": "0000", "Data": [] if self.calls == 1 else [position()]}

        with patch("reconcile_ttb.time.sleep"):
            with self.assertRaisesRegex(AccountInspectionError, "open positions"):
                reconcile(
                    ChangingAccount(), symbol="TXFA7", flag=self.flag,
                    audit_dir=self.audit, clear=True,
                    confirmations=(True, True, True, True),
                )
        self.assertTrue(self.flag.exists())

    def test_flag_replaced_during_inspection_must_not_be_removed(self):
        self.flag.write_text("error")
        class ChangingFlagAccount(FakeAccount):
            def query_orders(inner_self):
                inner_self.calls += 1
                if inner_self.calls == 2:
                    self.flag.write_text("new incident requires reconciliation")
                return {"Code": "0000", "Data": []}

        with patch("reconcile_ttb.time.sleep"):
            with self.assertRaisesRegex(AccountInspectionError, "changed during account inspection"):
                reconcile(
                    ChangingFlagAccount(), symbol="TXFA7", flag=self.flag,
                    audit_dir=self.audit, clear=True,
                    confirmations=(True, True, True, True),
                )
        self.assertTrue(self.flag.exists())

    def test_audit_failure_must_not_clear_flag(self):
        self.flag.write_text("error")
        with patch("ttb.reconciliation.append_audit", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                clear_reconciliation_flag(
                    self.flag,
                    first=inspect_account(FakeAccount(), "TXFA7"),
                    second=inspect_account(FakeAccount(), "TXFA7"),
                    audit_dir=self.audit,
                    confirm_virtual_account=True,
                    confirm_flat_account=True,
                    confirm_no_working_orders=True,
                    confirm_bot_stopped=True,
                )
        self.assertTrue(self.flag.exists())

    def test_config_guard_rejects_mismatched_symbol_or_shadow_mode(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "mode": "shadow", "symbol": "TXFA7",
            "confirmVirtualOrders": True, "confirmFlatAccount": True,
        }))
        with self.assertRaisesRegex(AccountInspectionError, "ttb mode"):
            _config(parse_args(["--config", str(config), "--clear-flag"]))
        config.write_text(json.dumps({
            "mode": "ttb", "symbol": "TXFA7",
            "confirmVirtualOrders": True, "confirmFlatAccount": True,
        }))
        with self.assertRaisesRegex(AccountInspectionError, "differs"):
            _config(parse_args(["--config", str(config), "--clear-flag", "--symbol", "TMFA7"]))


if __name__ == "__main__":
    unittest.main()
