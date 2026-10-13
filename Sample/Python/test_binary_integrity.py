from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from automation.check_binary_integrity import inspect

REPO = Path(__file__).resolve().parents[2]


class BinaryIntegrityTests(unittest.TestCase):
    def test_repo_snapshot_matches_declared_hashes_but_is_not_official_provenance(self):
        result = inspect(REPO)
        self.assertTrue(result["snapshot_verified"], result["errors"])
        self.assertFalse(result["official_provenance_verified"])
        self.assertEqual(len(result["artifacts"]), 2)

    def test_corrupted_binary_is_rejected_even_when_manifest_is_intact(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            manifest_file = REPO / "azure" / "runtime_binaries.json"
            content = json.loads(manifest_file.read_text(encoding="utf-8"))
            (folder / "azure").mkdir()
            (folder / "azure" / "runtime_binaries.json").write_text(
                json.dumps(content), encoding="utf-8",
            )
            for item in content["artifacts"]:
                target = folder / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / item["path"], target)
            self.assertTrue(inspect(folder)["snapshot_verified"])
            with (folder / "Sample" / "Python" / "TTBHelp.pyc").open("ab") as handle:
                handle.write(b"tampered")
            fail = inspect(folder)
            self.assertFalse(fail["snapshot_verified"])
            self.assertTrue(any("unexpected binary" in s for s in fail["errors"]))

    def test_missing_runtime_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "azure").mkdir()
            shutil.copyfile(
                REPO / "azure" / "runtime_binaries.json",
                folder / "azure" / "runtime_binaries.json",
            )
            self.assertFalse(inspect(folder)["snapshot_verified"])


if __name__ == "__main__":
    unittest.main()
