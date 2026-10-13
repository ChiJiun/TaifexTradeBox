from __future__ import annotations

import math

# Official TAIFEX index-futures contract multipliers (NTD per index point).
# https://www.taifex.com.tw/cht/2/tMF
# https://www.taifex.com.tw/cht/2/mTX
# https://www.taifex.com.tw/cht/2/tX
INDEX_MULTIPLIERS = {"TXF": 200.0, "MTX": 50.0, "TMF": 10.0}


def point_value_for(symbol: str, requested: float | None = None) -> float:
    """Never silently apply TXF's multiplier to micro/mini/unknown contracts."""
    code = symbol.upper().split(".")[0].strip()
    product = code[:3]
    official = INDEX_MULTIPLIERS.get(product)
    if requested is None:
        if official is None:
            raise ValueError(
                f"unknown contract {symbol}: --point-value must be explicitly configured"
            )
        return official
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("--point-value must be positive and finite")
    if official is not None and not math.isclose(requested, official, rel_tol=0, abs_tol=1e-9):
        raise ValueError(
            f"{product} contract multiplier is {official:g} NTD per point, not {requested:g}"
        )
    return requested
