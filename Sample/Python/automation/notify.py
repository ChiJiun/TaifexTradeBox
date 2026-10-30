"""Post fills and a daily account summary to a Discord/Slack webhook.

Run from Sample/Python every minute (the "TTB Notifier" scheduled task, via pyw.exe):

    python -m automation.notify

The webhook comes from the TTB_ALERT_WEBHOOK_URL user environment variable,
the same one health_watchdog.ps1 uses; without it nothing is sent. If
TTB_ALERT_MENTION_USER_ID holds a Discord user ID, the daily summary @s that
user (fills do not, there are too many of them). Fills are
read from the local trade database (refreshed from the execution journal
first), so a notification never depends on the bot process being alive.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from ttb.calendar import CalendarCoverageError, TradingCalendar
from ttb.trade_db import sync_journal

TAIPEI = timezone(timedelta(hours=8))
# After the 13:45 close, before the 14:00 scheduled VM stop.
SUMMARY_FROM = dtime(13, 46)
SUMMARY_UNTIL = dtime(14, 30)
MAX_FILL_LINES = 20
SIDE_NAMES = {"buy": "買", "sell": "賣"}


def post(url: str, text: str, mention_user_id: str = "") -> None:
    # content (Discord) / text (Slack); Discord rejects content over 2000 chars.
    text = text[:1900]
    payload: dict[str, Any] = {"content": text, "text": text}
    if mention_user_id:
        # Discord only; allowed_mentions pings just this user, never @everyone.
        payload["content"] = f"<@{mention_user_id}> {text}"
        payload["allowed_mentions"] = {"users": [mention_user_id]}
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()


def mention_user_id(raw: str) -> str:
    """A Discord user ID from the environment, or "" when unset or malformed."""
    raw = raw.strip()
    return raw if raw.isascii() and raw.isdigit() else ""


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def new_fills(db_path: Path, after_rowid: int) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(db_path)) as db:
        rows = db.execute(
            "SELECT rowid, symbol, trading_day, recorded_at, payload FROM events "
            "WHERE kind = 'fill' AND rowid > ? ORDER BY rowid", (after_rowid,),
        ).fetchall()
    return [{"rowid": rowid, "symbol": symbol, "trading_day": day, "recorded_at": at, **json.loads(payload)}
            for rowid, symbol, day, at, payload in rows]


def last_event_rowid(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as db:
        return int(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0])


def fill_line(fill: dict[str, Any]) -> str:
    try:
        stamp = datetime.fromisoformat(fill["recorded_at"]).astimezone(TAIPEI).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        stamp = str(fill.get("recorded_at", ""))
    side = SIDE_NAMES.get(str(fill.get("side")), str(fill.get("side")))
    return (f"{stamp} {fill['symbol']} {side} {fill.get('quantity')} 口 @ {fill.get('price')}"
            f"（委託 {fill.get('order_id')}）")


def fills_message(fills: list[dict[str, Any]]) -> str:
    lines = [fill_line(fill) for fill in fills[:MAX_FILL_LINES]]
    if len(fills) > MAX_FILL_LINES:
        lines.append(f"…另有 {len(fills) - MAX_FILL_LINES} 筆，見 output/trades.sqlite3")
    return "**成交通知**\n" + "\n".join(lines)


def day_fills(db_path: Path, trading_day: str) -> list[dict[str, Any]]:
    return [fill for fill in new_fills(db_path, 0) if fill["trading_day"] == trading_day]


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return 0.0


def twd_margin(response: Any) -> dict[str, Any]:
    rows = response.get("Data") if isinstance(response, dict) else None
    for row in rows or []:
        if isinstance(row, dict) and row.get("CURRENCY") == "TWD":
            return row
    raise RuntimeError(f"no TWD row in QUERYMARGIN reply: {str(response)[:200]}")


def summary_message(trading_day: str, fills: list[dict[str, Any]], margin: dict[str, Any] | None,
                    position: int | None, error: str = "") -> str:
    bought = sum(int(fill.get("quantity") or 0) for fill in fills if fill.get("side") == "buy")
    sold = sum(int(fill.get("quantity") or 0) for fill in fills if fill.get("side") == "sell")
    lines = [f"📊 **【每日結算】{trading_day}**", f"成交 {len(fills)} 筆（買 {bought} 口、賣 {sold} 口）"]
    if margin is not None:
        previous = _number(margin.get("PREV_BALN"))
        equity = _number(margin.get("ACCT_NETV_FUTS"))
        lines += [
            f"權益數 {equity:,.0f}（前日餘額 {previous:,.0f}）",
            f"當日損益 {equity - previous:+,.0f}，含手續費 {_number(margin.get('TRAN_FEE')):,.0f}、"
            f"交易稅 {_number(margin.get('TRAN_TAX')):,.0f}",
        ]
    if position is not None:
        lines.append(f"收盤部位 {position:+d} 口" if position else "收盤部位 0（已平倉）")
    if error:
        lines.append(f"⚠️ 無法向 TTB 取得帳務：{error}")
    return "\n".join(lines)


def account_snapshot(host: str, symbol: str) -> tuple[dict[str, Any], int]:
    from ttb.api import TTBApi
    from ttb.orders import reported_position

    api = TTBApi.connect(host)
    return twd_margin(api.query_margin()), reported_position(api.query_positions(), symbol)


def run(*, now: datetime, url: str, db_path: Path, journal_dir: Path, state_path: Path,
        calendar: TradingCalendar | None,
        snapshot: Callable[[], tuple[dict[str, Any], int]], send: Callable[..., None] = post,
        mention_user_id: str = "") -> list[str]:
    """One notifier pass; returns what was sent. State advances only after a send succeeds."""
    sync_journal(journal_dir, db_path)
    state = load_state(state_path)
    sent: list[str] = []
    if "fill_rowid" not in state:
        # First run: start from now instead of replaying every past fill.
        state["fill_rowid"] = last_event_rowid(db_path)
        save_state(state_path, state)
    fills = new_fills(db_path, int(state["fill_rowid"]))
    if fills:
        send(url, fills_message(fills))
        state["fill_rowid"] = fills[-1]["rowid"]
        save_state(state_path, state)
        sent.append(f"fills:{len(fills)}")

    local = now.astimezone(TAIPEI)
    today = local.date().isoformat()
    if (_is_trading_day(calendar, local.date()) and SUMMARY_FROM <= local.time() < SUMMARY_UNTIL
            and state.get("summary_day") != today):
        try:
            margin, position = snapshot()
            error = ""
        except Exception as exc:
            margin, position, error = None, None, f"{type(exc).__name__}: {exc}"
        send(url, summary_message(today, day_fills(db_path, today), margin, position, error), mention_user_id)
        state["summary_day"] = today
        save_state(state_path, state)
        sent.append("summary")
    return sent


def _is_trading_day(calendar: TradingCalendar | None, day: date) -> bool:
    if calendar is None:
        return day.weekday() < 5
    try:
        return calendar.is_trading_day(day)
    except CalendarCoverageError:
        return day.weekday() < 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post new fills and the daily summary to TTB_ALERT_WEBHOOK_URL")
    parser.add_argument("--journal-dir", type=Path, default=Path("output/journal"))
    parser.add_argument("--db", type=Path, default=Path("output/trades.sqlite3"))
    parser.add_argument("--state", type=Path, default=Path("output/supervisor/notify-state.json"))
    parser.add_argument("--calendar", type=Path, default=Path("automation/taifex_calendar_2026.json"))
    parser.add_argument("--config", type=Path, default=Path("automation/config.json"))
    parser.add_argument("--host", default="http://localhost:8080")
    args = parser.parse_args(argv)
    url = os.environ.get("TTB_ALERT_WEBHOOK_URL", "").strip()
    if not url:
        print(json.dumps({"ok": True, "sent": [], "reason": "TTB_ALERT_WEBHOOK_URL not set"}))
        return 0
    raw_mention = os.environ.get("TTB_ALERT_MENTION_USER_ID", "")
    mention = mention_user_id(raw_mention)
    try:
        calendar = TradingCalendar.from_file(args.calendar) if args.calendar.exists() else None
        now = datetime.now(TAIPEI)
        symbol = _symbol(args.config, now.date(), calendar)
        sent = run(now=now, url=url, db_path=args.db, journal_dir=args.journal_dir,
                   state_path=args.state, calendar=calendar,
                   snapshot=lambda: account_snapshot(args.host, symbol), mention_user_id=mention)
        if "summary" in sent and raw_mention.strip() and not mention:
            _log_error(args.state, "TTB_ALERT_MENTION_USER_ID is not a numeric Discord user ID; sent without mention")
    except Exception as exc:
        # pyw.exe (no console) runs this task, so errors go to a file.
        error = _log_error(args.state, f"{type(exc).__name__}: {exc}")
        print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "sent": sent}))
    return 0


def _log_error(state_path: Path, message: str) -> str:
    error = f"{datetime.now(TAIPEI).isoformat()} {message}"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with (state_path.parent / "notify-error.log").open("a", encoding="utf-8") as log:
        log.write(error + "\n")
    return error


def _symbol(config_path: Path, today: date, calendar: TradingCalendar | None) -> str:
    from ttb.calendar import resolve_symbol

    try:
        spec = json.loads(config_path.read_text(encoding="utf-8-sig")).get("symbol", "auto:TMF")
    except (OSError, ValueError, AttributeError):
        spec = "auto:TMF"
    return resolve_symbol(str(spec), today, calendar)


if __name__ == "__main__":
    raise SystemExit(main())
