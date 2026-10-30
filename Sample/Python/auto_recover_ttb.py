"""Unattended recovery for a TTB competition account used ONLY by this bot.

Cancels every working order, flattens supported futures with market IOC
orders (resending the unfilled rest a bounded number of times), verifies a
clean account twice, books the fills the bot never saw into its execution
journal so a same-day restart replays flat, and then clears
output/supervisor/manual-reconciliation-required.flag. Each action is audited
under output/reconciliation. Bounded by a durable per-day attempt budget.

Exit codes: 0 clean/recovered, 3 deferred (retry later), 2 refused (manual).
Never run this while sim_hft_bot.py is running; the supervisor guarantees it,
and the bot's lock on the journal directory makes this refuse if it is.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ttb.auto_recovery import AttemptBudget, RecoveryDeferred, RecoveryPolicy, RecoveryRefused, auto_recover
from ttb.calendar import TradingCalendar
from ttb.runtime import SessionSchedule


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--flag", type=Path,
                        default=Path("output/supervisor/manual-reconciliation-required.flag"))
    parser.add_argument("--audit-dir", type=Path, default=Path("output/reconciliation"))
    parser.add_argument("--budget-file", type=Path, default=Path("output/supervisor/auto-recovery.json"))
    parser.add_argument("--calendar-file", type=Path, default=Path("automation/taifex_calendar_2026.json"))
    parser.add_argument("--journal-dir", type=Path, default=Path("output/journal"),
                        help="the bot's execution journal directory (sim_hft_bot --journal-dir)")
    parser.add_argument("--max-attempts-per-day", type=int, default=3)
    parser.add_argument("--max-flatten-qty", type=int, default=10)
    parser.add_argument("--confirm-exclusive-bot-account", action="store_true",
                        help="required: nobody trades this TTB competition account manually")
    return parser.parse_args(argv)


def market_open(calendar_file: Path) -> bool:
    try:
        return SessionSchedule(calendar=TradingCalendar.from_file(calendar_file)).status().trading
    except Exception:
        return False


def journal_days(calendar_file: Path) -> tuple[str, ...]:
    """Trading days a bot restarted now may replay, with or without after-hours sessions."""
    try:
        calendar = TradingCalendar.from_file(calendar_file)
    except Exception:
        calendar = None
    return tuple(dict.fromkeys(
        SessionSchedule(calendar=calendar, include_after_hours=after_hours).status().trading_day
        for after_hours in (False, True)
    ))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.confirm_exclusive_bot_account:
        print(json.dumps({"ok": False, "error": "--confirm-exclusive-bot-account is required"}), file=sys.stderr)
        return 2
    try:
        from ttb.api import TTBApi
        api = TTBApi.connect(args.host, args.zmq_port)
        policy = RecoveryPolicy(max_attempts_per_day=args.max_attempts_per_day, max_flatten_qty=args.max_flatten_qty)
        result = auto_recover(
            api, flag=args.flag, audit_dir=args.audit_dir,
            budget=AttemptBudget(args.budget_file, policy.max_attempts_per_day),
            policy=policy, market_open=market_open(args.calendar_file),
            journal_dir=args.journal_dir, journal_days=journal_days(args.calendar_file),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RecoveryDeferred as exc:
        print(json.dumps({"ok": False, "deferred": True, "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"ok": False, "refused": True, "reason": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
