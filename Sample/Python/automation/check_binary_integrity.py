"""Validate exact repository TTB binary snapshot, not official provenance.

Run from Sample/Python:
    python -m automation.check_binary_integrity
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def inspect(root: Path) -> dict:
    root = root.resolve()
    path = root / "azure" / "runtime_binaries.json"
    result = {"snapshot_verified": False, "official_provenance_verified": False,
              "artifacts": [], "errors": []}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("artifacts"), list):
            raise ValueError("invalid binary manifest schema")
        if manifest.get("provenance_status") != "unverified_repository_snapshot":
            raise ValueError("manifest must not imply official source verification")
        for item in manifest["artifacts"]:
            relative = item["path"]
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                result["errors"].append(f"missing/outside repo: {relative}")
                continue
            size = target.stat().st_size
            with target.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            valid = size == item["size_bytes"] and digest == item["sha256"].lower()
            if item.get("type") == "python-bytecode":
                header = target.read_bytes()[:4].hex()
                valid = valid and header == item.get("pyc_magic_hex") and header == importlib.util.MAGIC_NUMBER.hex()
            elif item.get("type") == "python-source":
                # TTB's CONNECT handshake compares this string with TTB.exe.
                version = item.get("expected_version", "")
                valid = valid and bool(version) and f"'{version}'" in target.read_text(encoding="utf-8-sig")
            result["artifacts"].append({"path": relative, "snapshot_sha256_ok": valid})
            if not valid:
                result["errors"].append(f"unexpected binary hash/size/Python magic: {relative}")
        types = sorted(str(item.get("type")) for item in manifest["artifacts"])
        if len(result["artifacts"]) != 2 or len(types) != 2 or types[1] != "windows-executable":
            result["errors"].append("expected exactly TTB.exe and one TTBHelp (.py or .pyc)")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["errors"].append(f"manifest unreadable or invalid: {exc}")
    result["snapshot_verified"] = not result["errors"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify TTB binary snapshot hashes and Python bytecode magic")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    result = inspect(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["snapshot_verified"] else 2


if __name__ == "__main__":
    sys.exit(main())
