"""Isolated multi-symbol SHADOW-only runners with a fail-closed portfolio kill switch.

This does not implement safe multi-symbol TTB LIVE trading. In particular it
cannot validate aggregate broker margin/account identity or atomic orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ttb.contracts import point_value_for


class PortfolioSafetyError(RuntimeError):
    pass


class PortfolioRiskState:
    """Durable portfolio drawdown peak; corruption or config drift stops startup."""

    def __init__(self, path: Path, config: "PortfolioConfig") -> None:
        self.path = path
        self.config_id = hashlib.sha256(
            json.dumps({
                "instances": [asdict(i) for i in config.instances],
                "limits": asdict(config.limits),
            }, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.trading_day = ""
        self.peak = 0.0
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (data["version"] != 1 or data["config_id"] != self.config_id
                    or not isinstance(data["trading_day"], str)
                    or not isinstance(data["peak"], (int, float))
                    or not math.isfinite(float(data["peak"]))
                    or data["peak"] < 0):
                    raise ValueError("config changed or corrupt risk state")
                self.trading_day = data["trading_day"]
                self.peak = float(data["peak"])
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise PortfolioSafetyError("portfolio risk state is invalid; manual recovery required") from exc

    def peak_for(self, day: str) -> float:
        if self.trading_day and day < self.trading_day:
            raise PortfolioSafetyError("portfolio trading day moved backward")
        return self.peak if self.trading_day == day else 0.0

    def checkpoint(self, day: str, peak: float) -> None:
        self.peak_for(day)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"version": 1, "config_id": self.config_id,
                       "trading_day": day, "peak": peak}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        self.trading_day, self.peak = day, peak


@dataclass(frozen=True)
class PortfolioLimits:
    daily_loss_ntd: float
    max_drawdown_ntd: float
    max_abs_notional_ntd: float
    heartbeat_age_seconds: int = 45

    def validate(self) -> None:
        if (not all(math.isfinite(v) and v > 0 for v in
                    (self.daily_loss_ntd, self.max_drawdown_ntd,
                     self.max_abs_notional_ntd))
                or not 5 <= self.heartbeat_age_seconds <= 600):
            raise PortfolioSafetyError("positive, finite portfolio limits and valid heartbeat are required")


@dataclass(frozen=True)
class Instance:
    instance_id: str
    symbol: str
    status_port: int
    point_value: float
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioConfig:
    limits: PortfolioLimits
    instances: tuple[Instance, ...]


# Arguments that cannot override instance identity, file scopes or safety mode.
ALLOWED_EXTRA_ARGS = {
    "--strategy", "--strategy-module", "--strategy-params-json", "--fee-per-fill",
    "--tax-rate", "--slippage-ticks", "--target-ticks", "--stop-ticks",
    "--warmup-ticks", "--base-qty", "--max-order-qty", "--max-inventory",
    "--regular-open", "--regular-close", "--after-hours-open",
    "--after-hours-close", "--max-hold-seconds", "--half-width-ticks",
    "--quote-ttl-seconds", "--reprice-threshold-ticks",
    "--daily-loss-limit", "--max-drawdown",
}


def load_config(path: Path) -> PortfolioConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("instances"), list):
        raise PortfolioSafetyError("portfolio manifest must contain instances")
    limits_data = data.get("limits", {})
    try:
        limits = PortfolioLimits(
            float(limits_data["daily_loss_ntd"]),
            float(limits_data["max_drawdown_ntd"]),
            float(limits_data["max_abs_notional_ntd"]),
            int(limits_data.get("heartbeat_age_seconds", 45)),
        )
        limits.validate()
        instances = []
        used_ids, used_symbols, used_ports = set(), set(), set()
        for item in data["instances"]:
            name = str(item["id"])
            symbol = str(item["symbol"]).upper()
            port = int(item["status_port"])
            mode = item.get("mode", "shadow")
            point_value = float(item["point_value"])
            extra = item.get("extra_args", [])
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name)
                or not re.fullmatch(r"[A-Z0-9]{4,12}", symbol)
                or mode != "shadow"
                or not 1024 <= port <= 65535
                or not math.isfinite(point_value) or point_value <= 0
                or not isinstance(extra, list) or len(extra) % 2
                or any(not isinstance(x, str) for x in extra)
                or any(extra[i] not in ALLOWED_EXTRA_ARGS
                       for i in range(0, len(extra), 2))):
                raise PortfolioSafetyError("invalid/shadow-unsafe instance settings")
            try:
                point_value_for(symbol, point_value)
            except ValueError as exc:
                raise PortfolioSafetyError(str(exc)) from exc
            # Never allow duplicate scopes or status ports. Status server
            # itself defaults to loopback-only.
            if name in used_ids or symbol in used_symbols or port in used_ports:
                raise PortfolioSafetyError("duplicate instance id, symbol or status port")
            used_ids.add(name)
            used_symbols.add(symbol)
            used_ports.add(port)
            instances.append(Instance(name, symbol, port, point_value, tuple(extra)))
        if not 2 <= len(instances) <= 10:
            raise PortfolioSafetyError("portfolio requires 2-10 isolated shadow symbols")
        return PortfolioConfig(limits, tuple(instances))
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise PortfolioSafetyError("invalid portfolio manifest") from exc


def runtime_paths(root: Path, instance: Instance) -> dict[str, Path]:
    base = root / "instances" / instance.instance_id
    return {
        "root": base,
        "output": base / "runs",
        "health": base / "health.json",
        "risk": base / "risk-state.json",
        "journal": base / "journal",
        "rates": base / "rates.json",
        "stop": base / "supervisor" / "stop.requested",
        "log": base / "supervisor" / "bot.log",
    }


def child_command(instance: Instance, root: Path, python: str | None = None) -> list[str]:
    files = runtime_paths(root, instance)
    return [
        python or sys.executable, "sim_hft_bot.py",
        "--symbol", instance.symbol, "--mode", "shadow",
        "--point-value", str(instance.point_value),
        "--output", str(files["output"]),
        "--health-file", str(files["health"]),
        "--risk-state-file", str(files["risk"]),
        "--journal-dir", str(files["journal"]),
        "--rate-counters-file", str(files["rates"]),
        "--stop-file", str(files["stop"]),
        "--log-file", str(files["log"]),
        "--status-host", "127.0.0.1",
        "--status-port", str(instance.status_port),
        *instance.extra_args,
    ]


def _quote_age_seconds(health: dict[str, Any], now: datetime) -> float:
    last = datetime.fromisoformat(health["last_quote_wall_utc"])
    if last.tzinfo is None:
        raise ValueError("last_quote_wall_utc must include timezone")
    return (now - last.astimezone(timezone.utc)).total_seconds()


def evaluate_portfolio(
    config: PortfolioConfig,
    root: Path,
    *,
    now: datetime,
    peak_equity: float = 0.0,
) -> dict[str, Any]:
    """No stale/missing health, unknown PnL or duplicate symbol is acceptable."""
    marked = 0.0
    notional = 0.0
    details = []
    trading_days: set[str] = set()
    reason = ""
    for item in config.instances:
        path = runtime_paths(root, item)["health"]
        if not path.is_file():
            raise PortfolioSafetyError(f"{item.instance_id}: health file missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(value["updated_at"])
            if updated.tzinfo is None:
                raise ValueError("naive updated_at")
            age = (now - updated.astimezone(timezone.utc)).total_seconds()
            quote_age = _quote_age_seconds(value, now)
            position = int(value["position"])
            pnl = float(value["marked_pnl"])
            last_price = float(value["last_price"])
            day = str(value["trading_day"])
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
                raise ValueError("invalid trading day")
            trading_days.add(day)
            if (value["symbol"] != item.symbol or value["mode"] != "shadow"
                or value["state"] in ("error", "manual_reconciliation_required", "stale_market_data")
                or age < -10 or age > config.limits.heartbeat_age_seconds
                or quote_age < -10 or quote_age > config.limits.heartbeat_age_seconds
                or not math.isfinite(pnl) or not math.isfinite(last_price)
                or last_price <= 0 or isinstance(value["position"], bool)):
                raise ValueError("invalid / stale health state")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PortfolioSafetyError(f"{item.instance_id}: invalid/stale health; halt all instances") from exc
        marked += pnl
        notional += abs(position) * last_price * item.point_value
        details.append({"instance_id": item.instance_id, "symbol": item.symbol,
                        "position": position, "marked_pnl": pnl,
                        "notional_ntd": abs(position) * last_price * item.point_value})
    if len(trading_days) != 1:
        raise PortfolioSafetyError("symbols have inconsistent trading days; halt all instances")
    peak = max(peak_equity, marked)
    if marked <= -config.limits.daily_loss_ntd:
        reason = "portfolio_daily_loss_limit"
    elif peak - marked >= config.limits.max_drawdown_ntd:
        reason = "portfolio_max_drawdown"
    elif notional >= config.limits.max_abs_notional_ntd:
        reason = "portfolio_notional_limit"
    return {"trading_day": next(iter(trading_days)),
            "marked_pnl_ntd": marked, "gross_notional_ntd": notional,
            "peak_marked_pnl_ntd": peak, "kill_reason": reason,
            "instances": details}


def awaiting_initial_quotes(config: PortfolioConfig, root: Path, now: datetime) -> bool:
    """Report not-ready while a shadow symbol lacks its first quote.

    This is NOT a risk-pass: there is no validated aggregate PnL until each
    symbol has provided a quote. Child processes are still checked for exit,
    stale heartbeat and explicit error state during the waiting period.
    """
    pending = False
    for instance in config.instances:
        path = runtime_paths(root, instance)["health"]
        if not path.exists():
            raise PortfolioSafetyError(f"{instance.instance_id}: health not available")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            age = (now - datetime.fromisoformat(data["updated_at"])).total_seconds()
            if (data["symbol"] != instance.symbol or data["mode"] != "shadow"
                or data["state"] in ("error", "manual_reconciliation_required",
                                      "stale_market_data", "stopped")
                or not -10 <= age <= config.limits.heartbeat_age_seconds):
                raise ValueError("unhealthy pre-quote child")
            # A healthy-looking heartbeat says only the process is alive.
            # It does NOT prove an old last_price can still value inventory.
            quote_fresh = False
            if "last_price" in data and "last_quote_wall_utc" in data:
                quote_age = _quote_age_seconds(data, now)
                quote_fresh = -10 <= quote_age <= config.limits.heartbeat_age_seconds
            if not quote_fresh:
                if data["state"] not in ("starting", "running", "waiting_market"):
                    raise ValueError("cannot value pending child")
                if type(data.get("position", 0)) is not int or data.get("position", 0) != 0:
                    raise ValueError("unvalued open position")
                pending = True
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PortfolioSafetyError(
                f"{instance.instance_id}: bad initial health, stop shadow portfolio"
            ) from exc
    return pending


def persist_portfolio_health(root: Path, state: dict[str, Any]) -> Path:
    """Atomic aggregate snapshot for local health/watchdog inspection."""
    root.mkdir(parents=True, exist_ok=True)
    dest = root / "portfolio-health.json"
    temp = root / "portfolio-health.tmp"
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({
                "mode": "shadow_portfolio",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **state,
            }, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
    return dest


def request_global_stop(config: PortfolioConfig, root: Path, reason: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Write the latch BEFORE per-instance stop requests; a reboot must not
    # silently restore a previously breached portfolio limit.
    latch = root / "portfolio-halt.flag"
    with latch.open("x", encoding="utf-8") as handle:
        handle.write(reason + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    for instance in config.instances:
        stop = runtime_paths(root, instance)["stop"]
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text(reason + "\n", encoding="utf-8")


def run(config: PortfolioConfig, root: Path, interval: float = 5.0) -> None:
    if interval < 1:
        raise PortfolioSafetyError("polling interval must be >=1s")
    if (root / "portfolio-halt.flag").exists():
        raise PortfolioSafetyError("portfolio halt latch exists; manual recovery required")
    risk_store = PortfolioRiskState(root / "portfolio-risk-state.json", config)
    children: list[subprocess.Popen] = []
    started = time.monotonic()
    try:
        for instance in config.instances:
            paths = runtime_paths(root, instance)
            paths["root"].mkdir(parents=True, exist_ok=True)
            paths["stop"].unlink(missing_ok=True)
            paths["log"].parent.mkdir(parents=True, exist_ok=True)
            children.append(subprocess.Popen(
                child_command(instance, root), cwd=Path(__file__).resolve().parents[1],
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            ))
        while True:
            if any(proc.poll() is not None for proc in children):
                raise PortfolioSafetyError("a child process exited; stop all portfolio instances")
            if time.monotonic() - started >= 20:
                now = datetime.now(timezone.utc)
                if awaiting_initial_quotes(config, root, now):
                    persist_portfolio_health(root, {
                        "state": "waiting_first_quotes",
                        "risk_evaluation_complete": False,
                        "note": "no trading readiness assertion; one or more symbols unvalued",
                    })
                    time.sleep(interval)
                    continue
                observed = evaluate_portfolio(
                    config, root, now=now,
                    peak_equity=0,
                )
                # Apply the persisted running-day peak before evaluating
                # drawdown; an orchestration restart must not reset limits.
                previous_peak = risk_store.peak_for(observed["trading_day"])
                state = evaluate_portfolio(
                    config, root, now=now,
                    peak_equity=previous_peak,
                )
                risk_store.checkpoint(state["trading_day"], state["peak_marked_pnl_ntd"])
                persist_portfolio_health(root, {
                    "state": "monitoring",
                    "risk_evaluation_complete": True,
                    **state,
                })
                if state["kill_reason"]:
                    raise PortfolioSafetyError(state["kill_reason"])
            time.sleep(interval)
    except BaseException as exc:
        reason = f"portfolio_stop: {exc}"
        if not (root / "portfolio-halt.flag").exists():
            request_global_stop(config, root, reason)
        raise PortfolioSafetyError(reason) from exc
    finally:
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.terminate()  # shadow only; no broker-side order exposure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed multi-symbol SHADOW orchestration; never supports live")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("output/portfolio"))
    parser.add_argument("--run", action="store_true", help="launch shadow instances (default: validate/dry run)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        commands = [
            {"instance_id": instance.instance_id,
             "argv": child_command(instance, args.root)}
            for instance in config.instances
        ]
        if not args.run:
            print(json.dumps({"mode": "dry_run_shadow_only",
                              "commands": commands}, ensure_ascii=False, indent=2))
            return 0
        run(config, args.root)
    except (PortfolioSafetyError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
