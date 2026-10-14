from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell/Task Scheduler audit")
class VmReadinessAuditTests(unittest.TestCase):
    def run_audit(self, directory):
        script = Path(__file__).parent / "automation" / "audit_vm_readiness.ps1"
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-WorkDir", directory],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=25,
        )
        return result, json.loads(result.stdout)

    def test_fresh_heartbeat_with_stale_quote_is_not_trading_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            health_file = Path(directory) / "output" / "health.json"
            health_file.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            row = {
                "symbol": "TMFJ6", "mode": "shadow", "state": "healthy",
                "position": 0, "updated_at": now.isoformat(),
                "last_quote_wall_utc": (now - timedelta(hours=4)).isoformat(),
            }
            health_file.write_text(json.dumps(row), encoding="utf-8")
            proc, report = self.run_audit(directory)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertTrue(report["health"]["heartbeatFresh"])
            self.assertFalse(report["health"]["acceptedQuoteFresh"])
            self.assertFalse(report["tradingReady"])
            self.assertIn("accepted_quote_missing_or_stale", report["blockers"])
            row["state"] = "waiting_market"
            health_file.write_text(json.dumps(row), encoding="utf-8")
            _proc, closed = self.run_audit(directory)
            self.assertFalse(closed["tradingReady"])
            self.assertNotIn("accepted_quote_missing_or_stale", closed["blockers"])
            row["position"] = 1
            health_file.write_text(json.dumps(row), encoding="utf-8")
            _proc, holding = self.run_audit(directory)
            self.assertIn("unvalued_position_during_market_close", holding["blockers"])

    def test_code_feature_drift_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ttb").mkdir(parents=True)
            (root / "sim_hft_bot.py").write_text("# old runtime", encoding="utf-8")
            (root / "ttb" / "engine.py").write_text("# old engine", encoding="utf-8")
            (root / "ttb" / "market_data.py").write_text("# old quote feed", encoding="utf-8")
            _proc, report = self.run_audit(directory)
            self.assertTrue(all(not status for status in report["runtimeSafetyFeatures"].values()))
            self.assertIn("missing_runtime_feature_staleFatalGuard", report["blockers"])
            self.assertIn("missing_runtime_feature_quoteSequenceIntegrity", report["blockers"])

            (root / "sim_hft_bot.py").write_text(
                "# --stale-fatal-seconds --max-exchange-lag-seconds", encoding="utf-8")
            (root / "ttb" / "engine.py").write_text(
                "# stale_fatal_seconds last_quote_wall_utc", encoding="utf-8")
            (root / "ttb" / "market_data.py").write_text(
                "# max_exchange_lag_seconds\nclass MarketDataIntegrity: pass", encoding="utf-8")
            _proc, newer = self.run_audit(directory)
            self.assertTrue(all(newer["runtimeSafetyFeatures"].values()))
            self.assertNotIn("missing_runtime_feature_staleFatalGuard", newer["blockers"])
            self.assertFalse(newer["safeForSupervisedShadowSoak"])

    def test_missing_runtime_files_return_structured_nonready(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(__file__).parent / "automation" / "audit_vm_readiness.ps1"
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), "-WorkDir", directory],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=25,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["schema"], "ttb-vm-readiness-v1")
            self.assertFalse(report["safeForSupervisedShadowSoak"])
            self.assertFalse(report["supervisedLiveTradingApproved"])
            self.assertFalse(report["requiredFiles"]["captureCli"])
            self.assertIn("health_missing_or_unreadable", report["blockers"])
            self.assertIn("watchdog", report["tasks"])
            self.assertFalse(report["runtimeSafetyFeatures"]["staleFatalGuard"])


if __name__ == "__main__":
    unittest.main()
