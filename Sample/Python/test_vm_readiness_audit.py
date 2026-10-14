from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell/Task Scheduler audit")
class VmReadinessAuditTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
