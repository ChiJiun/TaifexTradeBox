"""Lightweight documentation drift gate using the actual CLI parser defaults.

Run from Sample/Python: python -m automation.check_docs_consistency
"""
from __future__ import annotations

import sys
from pathlib import Path

from sim_hft_bot import parse_args

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
BEGIN = "<!-- CLI DEFAULTS BEGIN -->"
END = "<!-- CLI DEFAULTS END -->"
PROPERTIES = (
    ("mode", "--mode"),
    ("strategy", "--strategy"),
    ("regular_open", "--regular-open"),
    ("regular_close", "--regular-close"),
    ("after_hours_open", "--after-hours-open"),
    ("after_hours_close", "--after-hours-close"),
    ("quote_stale_seconds", "--quote-stale-seconds"),
    ("stale_fatal_seconds", "--stale-fatal-seconds"),
    ("resume_fresh_ticks", "--resume-fresh-ticks"),
    ("flatten_on_shutdown", "--flatten-on-shutdown"),
    ("daily_loss_limit", "--daily-loss-limit"),
    ("max_drawdown", "--max-drawdown"),
    ("point_value", "--point-value"),
)


def render_cli_table() -> str:
    args = parse_args(["--symbol", "TMFJ6"])
    result = ["| CLI 參數 | 預設 |", "|---|---|"]
    for attr, flag in PROPERTIES:
        result.append(f"| `{flag}` | `{getattr(args, attr)}` |")
    return "\n".join(result)


def check() -> list[str]:
    failures = []
    operations = ROOT / "OPERATIONS.md"
    if not operations.is_file():
        return ["OPERATIONS.md missing"]
    text = operations.read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        failures.append("OPERATIONS.md: exactly one CLI defaults block required")
    else:
        actual = text.split(BEGIN, 1)[1].split(END, 1)[0].strip()
        if actual != render_cli_table():
            failures.append("OPERATIONS.md: CLI defaults differ from sim_hft_bot.parse_args")
    for needed in (
        "OPERATOR_RUNBOOK.md", "manual-reconciliation-required.flag",
        "risk-state.json", "Journal", "#2", "#4",
        "--flatten-on-shutdown", "--stale-fatal-seconds",
    ):
        if needed not in text:
            failures.append(f"OPERATIONS.md lacks required operator guidance: {needed}")
    strategy = (ROOT / "MARKET_MAKER.md").read_text(encoding="utf-8")
    for forbidden in ("沒有跨重啟持久化", "尚無假日與到期日行事曆", "Ctrl+C 只撤"):
        if forbidden in strategy:
            failures.append(f"MARKET_MAKER.md contains obsolete claim: {forbidden}")
    if "OPERATIONS.md" not in strategy or "walk_forward_oos.py" not in strategy:
        failures.append("MARKET_MAKER.md must link runtime runbook and research pipeline")
    azure = (REPO / "azure" / "README.md").read_text(encoding="utf-8")
    if "--confirm-bot-stopped" not in azure or "OPERATIONS.md" not in azure:
        failures.append("Azure README must link operator runbook and guarded probe command")
    return failures


def main() -> int:
    errors = check()
    if errors:
        for item in errors:
            print(f"DOCUMENTATION DRIFT: {item}", file=sys.stderr)
        return 1
    print("Documentation consistency OK: source parser matches operational defaults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
