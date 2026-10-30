from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ttb.api import TTBApi
from ttb.reconciliation import (
    AccountInspectionError,
    append_audit,
    audit_snapshot,
    clear_reconciliation_flag,
    inspect_account,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only TTB account reconciliation and guarded operator latch clearing"
    )
    parser.add_argument("--config", type=Path, default=Path("automation/config.json"))
    parser.add_argument("--symbol", default="", help="override symbol for read-only inspection")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument(
        "--flag",
        type=Path,
        default=Path("output/supervisor/manual-reconciliation-required.flag"),
    )
    parser.add_argument("--audit-dir", type=Path, default=Path("output/reconciliation"))
    parser.add_argument("--clear-flag", action="store_true",
                        help="remove the local live-startup block only after explicit confirmation")
    parser.add_argument("--confirm-virtual-account", action="store_true",
                        help="operator verified the intended competition/account in TTB GUI")
    parser.add_argument("--confirm-flat-account", action="store_true",
                        help="operator verified there are no open positions in TTB GUI")
    parser.add_argument("--confirm-no-working-orders", action="store_true",
                        help="operator verified all working orders have been resolved in TTB GUI")
    parser.add_argument("--confirm-bot-stopped", action="store_true",
                        help="operator verified no live bot is running")
    return parser.parse_args(argv)


def _config(args: argparse.Namespace) -> tuple[str, str]:
    if not args.config.is_file():
        if args.clear_flag:
            raise AccountInspectionError("config.json required to clear the live reconciliation flag")
        if not args.symbol.strip():
            raise AccountInspectionError("provide --symbol or an existing automation/config.json")
        return args.symbol.upper(), "unknown"

    try:
        obj = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountInspectionError("cannot read a valid automation config") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("symbol"), str) or not obj["symbol"].strip():
        raise AccountInspectionError("automation config missing symbol")
    symbol = obj["symbol"].strip().upper()
    mode = obj.get("mode")
    if args.clear_flag:
        if mode != "ttb":
            raise AccountInspectionError("only ttb mode may clear a live reconciliation flag")
        if args.symbol and args.symbol.upper() != symbol:
            raise AccountInspectionError("--symbol differs from active automation config")
        if not obj.get("confirmVirtualOrders") or not obj.get("confirmFlatAccount"):
            raise AccountInspectionError("live confirmation flags are not enabled in config")
    return (args.symbol.upper() or symbol), str(mode)


def reconcile(
    api: Any,
    *,
    symbol: str,
    flag: Path,
    audit_dir: Path,
    clear: bool = False,
    confirmations: tuple[bool, bool, bool, bool] = (False, False, False, False),
    delay_seconds: float = 0.5,
) -> dict[str, Any]:
    """No exchange writes. The only optional mutation is deleting the local flag."""
    action = "read_only"
    try:
        flag_identity = None
        if clear:
            if not flag.is_file():
                raise AccountInspectionError("manual reconciliation flag does not exist")
            old_flag = flag.stat()
            flag_identity = (old_flag.st_ino, old_flag.st_mtime_ns, old_flag.st_size)
        first = inspect_account(api, symbol)
        if not clear:
            audit_path = append_audit(audit_dir, audit_snapshot(first, action=action))
            return {
                "ok": first.can_clear,
                "action": action,
                "flag_present": flag.is_file(),
                "account": first.__dict__,
                "audit_file": str(audit_path),
            }
        if not all(confirmations):
            raise AccountInspectionError("clear-flag requires four explicit operator confirmations")
        if not flag.is_file():
            raise AccountInspectionError("manual reconciliation flag does not exist")
        if not first.can_clear:
            raise AccountInspectionError("first inspection found open positions or working orders")
        time.sleep(max(0.1, delay_seconds))
        second = inspect_account(api, symbol)
        audit_path = clear_reconciliation_flag(
            flag,
            first=first,
            second=second,
            audit_dir=audit_dir,
            confirm_virtual_account=confirmations[0],
            confirm_flat_account=confirmations[1],
            confirm_no_working_orders=confirmations[2],
            confirm_bot_stopped=confirmations[3],
            expected_flag_identity=flag_identity,
        )
        return {
            "ok": True,
            "action": "flag_cleared",
            "flag_present": flag.is_file(),
            "account": second.__dict__,
            "audit_file": str(audit_path),
        }
    except Exception as exc:
        # Do not store raw TTB responses or potentially private account details.
        # If an audit cannot be written, the latch is still never cleared.
        try:
            append_audit(
                audit_dir,
                {"action": "blocked", "symbol": symbol.upper(),
                 "requested_clear": clear, "reason": str(exc)},
            )
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        symbol, _mode = _config(args)
        if args.clear_flag and urlparse(args.host).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AccountInspectionError("--clear-flag requires a loopback TTB API host")
        if args.clear_flag and not all((
            args.confirm_virtual_account,
            args.confirm_flat_account,
            args.confirm_no_working_orders,
            args.confirm_bot_stopped,
        )):
            raise AccountInspectionError("all four --confirm-* flags required for --clear-flag")
        api = TTBApi.connect(args.host, args.zmq_port)
        result = reconcile(
            api,
            symbol=symbol,
            flag=args.flag,
            audit_dir=args.audit_dir,
            clear=args.clear_flag,
            confirmations=(
                args.confirm_virtual_account,
                args.confirm_flat_account,
                args.confirm_no_working_orders,
                args.confirm_bot_stopped,
            ),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
