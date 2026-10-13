from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ttb.api import TTBApi, is_success_response
from ttb.orders import _recursive_json, _response_rows


class CaptureError(RuntimeError):
    pass


ORDER_FIELDS = {
    "ORDNO", "ORDR_PRCE", "VOLM", "LESS_VOLM", "DEAL_TOTL", "DEAL_PRCE",
    "COMD_ID", "ComdID1", "BS", "STAT", "Status", "OrderQty",
    "RemainQty", "RemainingQty", "FilledQty", "Side", "Price", "Symbol",
}
POSITION_FIELDS = {
    "Hold", "HOLD", "PositionQty", "Qty", "ComdID1", "ComdID2",
    "COMD_ID", "Symbol", "ComdBS1", "ComdBS2", "BS", "Side",
}
HASH_FIELDS = {"ordno", "orderno", "order_no"}
ALLOWED_STATUS = {"STATUS", "STAT"}


def sanitized_response(endpoint: str, raw: Any, salt: str) -> dict[str, Any]:
    """Preserve verified *shapes*, omit account/user/financial secrets.

    Include no raw account IDs, order IDs, error bodies, or raw margin amounts.
    Correlation of an order within one capture is via a keyed salted hash.
    """
    if endpoint not in {"orders", "positions", "margin"}:
        raise ValueError("unsupported endpoint")
    if not is_success_response(raw):
        return {"success": False, "code": str(raw.get("Code", ""))[:8] if isinstance(raw, dict) else ""}
    if endpoint == "margin":
        body = _recursive_json(raw.get("Data", raw.get("data"))) if isinstance(raw, dict) else None
        rows = body if isinstance(body, list) else [body]
        return {
            "success": True,
            "data_type": type(body).__name__,
            "fields": [
                {str(key)[:80]: type(value).__name__
                 for key, value in row.items()
                 if isinstance(key, str) and len(key) <= 80}
                for row in rows if isinstance(row, dict)
            ],
        }
    rows = _response_rows(raw)
    allowed = ORDER_FIELDS if endpoint == "orders" else POSITION_FIELDS
    samples: list[dict[str, Any]] = []
    for row in rows[:100]:
        sample: dict[str, Any] = {}
        for key, value in row.items():
            if key not in allowed or not isinstance(value, (str, int, float, type(None))):
                continue
            if key.lower() in HASH_FIELDS and value not in (None, ""):
                sample[key] = "hash:" + hashlib.sha256(
                    (salt + "|" + str(value)).encode("utf-8")
                ).hexdigest()[:16]
            elif key in {"COMD_ID", "ComdID1", "ComdID2", "Symbol"}:
                # Product symbols are public; strip unexpected freeform text.
                text = str(value).upper().split(".")[0]
                sample[key] = text if text.isalnum() and len(text) <= 12 else "<redacted>"
            elif key in {"STAT", "Status"}:
                # Status descriptions may include free-form identity details.
                text = str(value)
                sample[key] = text if text in {
                    "委託成功", "完全成交", "部分成交", "取消", "刪單", "已取消",
                    "委託失敗", "成交", "已成交", "Canceled", "Filled",
                } else "<unrecognized-status>"
            else:
                # Numeric order counts/prices and sides are useful for schema tests.
                value_text = str(value)
                sample[key] = value if len(value_text) <= 32 else "<redacted>"
        samples.append(sample)
    return {
        "success": True, "data_type": "list",
        "row_count": len(rows),
        "sample_truncated": len(rows) > len(samples),
        "rows": samples,
    }


def capture(api: TTBApi) -> dict[str, Any]:
    salt = secrets.token_hex(16)
    return {
        "format": "ttb-sanitized-readonly-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account_identity": "UNVERIFIED_MANUALLY_IN_GUI",
        "margin": sanitized_response("margin", api.query_margin(), salt),
        "orders": sanitized_response("orders", api.query_orders(), salt),
        "positions": sanitized_response("positions", api.query_positions(), salt),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only sanitized TTB API response capture; NEVER submits or cancels orders")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--output-dir", type=Path, default=Path("output") / "api-captures")
    args = parser.parse_args(argv)
    try:
        if args.output_dir.is_symlink():
            raise CaptureError("output directory cannot be a symlink")
        data = capture(TTBApi.connect(args.host, args.zmq_port))
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = args.output_dir / f"ttb-readonly-{stamp}.json"
        with dest.open("x", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        print(json.dumps({"ok": True, "capture": str(dest), "account_identity_verified": False}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": "read-only capture failed; inspect TTB GUI locally"}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
