"""Post-close charts and trade records for review.

Builds, for one trading day, from the local trade database and the bot's quote
CSVs (output/runs/**/*.csv):

- report-<day>.png: price per traded contract with every fill and closed round
  trip, the day's cumulative net P&L, and the daily and cumulative net P&L
  since the first recorded day.
- fills-<day>.csv: every fill of the day.
- roundtrips-<day>.csv: fills paired first-in first-out into closed round trips.

The notifier calls build_report after the daily summary; run it by hand with

    python -m automation.daily_report --day 2026-10-30

Net P&L is computed from fills with --fee-per-fill and --tax-rate, so it can
differ from TTB's equity by fee rounding; the summary message carries TTB's own
figure.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ttb.contracts import point_value_for

TAIPEI = timezone(timedelta(hours=8))
FONTS = ["Microsoft JhengHei", "Microsoft YaHei", "PMingLiU", "Noto Sans CJK TC", "DejaVu Sans"]


@dataclass(frozen=True)
class Fill:
    symbol: str
    trading_day: str
    at: datetime
    side: str
    price: float
    quantity: int
    order_id: str


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    direction: int  # +1 long, -1 short
    quantity: int
    opened_at: datetime
    entry: float
    closed_at: datetime
    exit: float
    gross: float
    cost: float
    trading_day: str = ""

    @property
    def net(self) -> float:
        return self.gross - self.cost


@dataclass
class Costs:
    fee_per_fill: float = 50.0
    tax_rate: float = 0.00002

    def per_lot(self, symbol: str, price: float) -> float:
        return self.fee_per_fill + self.tax_rate * price * point_value_for(symbol)


@dataclass
class Book:
    """FIFO open lots per contract; turns fills into closed round trips."""

    costs: Costs
    lots: dict[str, deque[list[Any]]] = field(default_factory=lambda: defaultdict(deque))
    closed: list[RoundTrip] = field(default_factory=list)
    open_cost: float = 0.0

    def apply(self, fill: Fill) -> list[RoundTrip]:
        sign = 1 if fill.side == "buy" else -1
        lots = self.lots[fill.symbol]
        remaining = fill.quantity
        done: list[RoundTrip] = []
        pv = point_value_for(fill.symbol)
        while remaining and lots and lots[0][0] * sign < 0:
            lot = lots[0]  # [signed qty, price, opened_at, entry cost per lot]
            qty = min(remaining, abs(lot[0]))
            direction = -sign
            done.append(RoundTrip(
                fill.symbol, direction, qty, lot[2], lot[1], fill.at, fill.price,
                gross=(fill.price - lot[1]) * direction * pv * qty,
                cost=(lot[3] + self.costs.per_lot(fill.symbol, fill.price)) * qty,
                trading_day=fill.trading_day,
            ))
            self.open_cost -= lot[3] * qty
            lot[0] += sign * qty
            remaining -= qty
            if lot[0] == 0:
                lots.popleft()
        if remaining:
            entry_cost = self.costs.per_lot(fill.symbol, fill.price)
            lots.append([sign * remaining, fill.price, fill.at, entry_cost])
            self.open_cost += entry_cost * remaining
        self.closed.extend(done)
        return done

    def position(self, symbol: str) -> int:
        return sum(lot[0] for lot in self.lots[symbol])

    def unrealized(self, marks: dict[str, float]) -> float:
        total = 0.0
        for symbol, lots in self.lots.items():
            if symbol in marks:
                pv = point_value_for(symbol)
                total += sum(lot[0] * (marks[symbol] - lot[1]) * pv for lot in lots)
        return total - self.open_cost


def load_fills(db_path: Path) -> list[Fill]:
    with closing(sqlite3.connect(db_path)) as db:
        rows = db.execute(
            "SELECT symbol, trading_day, recorded_at, payload FROM events WHERE kind = 'fill' ORDER BY rowid"
        ).fetchall()
    fills = []
    for symbol, day, recorded_at, payload in rows:
        data = json.loads(payload)
        fills.append(Fill(
            symbol, day, datetime.fromisoformat(recorded_at).astimezone(TAIPEI), str(data.get("side")),
            float(data.get("price")), int(data.get("quantity") or 0), str(data.get("order_id") or ""),
        ))
    return sorted(fills, key=lambda f: f.at)


def load_prices(runs_dir: Path, day: str) -> dict[str, list[tuple[datetime, float]]]:
    """Last trade price per contract from every quote CSV the bot wrote that day."""
    prices: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for path in sorted(runs_dir.rglob("*.csv")):
        if day.replace("-", "") not in path.name:
            continue
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                try:
                    at = datetime.strptime(row["wall_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)
                    last = float(row["last"])
                except (KeyError, TypeError, ValueError):
                    continue
                if last > 0 and at.date().isoformat() == day:
                    prices[row["symbol"]].append((at, last))
    return {symbol: sorted(series) for symbol, series in prices.items()}


def daily_net(fills: Iterable[Fill], costs: Costs) -> dict[str, float]:
    """Net P&L of round trips closed on each trading day."""
    book = Book(costs)
    totals: dict[str, float] = {}
    for fill in fills:
        totals.setdefault(fill.trading_day, 0.0)
        for trip in book.apply(fill):
            totals[fill.trading_day] += trip.net
    return dict(sorted(totals.items()))


def intraday_curve(fills: list[Fill], prices: dict[str, list[tuple[datetime, float]]], day: str,
                   costs: Costs) -> list[tuple[datetime, float]]:
    """Net P&L since the open (closed round trips plus open lots at the last price)."""
    book = Book(costs)
    for fill in fills:
        if fill.trading_day < day:
            book.apply(fill)
    carried = sum(trip.net for trip in book.closed)
    events: list[tuple[datetime, int, Any]] = [(f.at, 0, f) for f in fills if f.trading_day == day]
    events += [(at, 1, (symbol, last)) for symbol, series in prices.items() for at, last in series]
    marks: dict[str, float] = {}
    curve = []
    for at, kind, item in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == 0:
            book.apply(item)
            marks.setdefault(item.symbol, item.price)
        else:
            marks[item[0]] = item[1]
        realized = sum(trip.net for trip in book.closed) - carried
        curve.append((at, realized + book.unrealized(marks)))
    return curve


def write_fills(path: Path, fills: list[Fill]) -> None:
    position: dict[str, int] = defaultdict(int)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["時間", "商品", "買賣", "價格", "口數", "成交後部位", "委託書號"])
        for fill in fills:
            position[fill.symbol] += fill.quantity if fill.side == "buy" else -fill.quantity
            writer.writerow([fill.at.strftime("%Y-%m-%d %H:%M:%S"), fill.symbol,
                             "買" if fill.side == "buy" else "賣", fill.price, fill.quantity,
                             position[fill.symbol], fill.order_id])


def write_round_trips(path: Path, trips: list[RoundTrip]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["商品", "方向", "口數", "進場時間", "進場價", "出場時間", "出場價",
                         "點數", "毛損益", "手續費稅", "淨損益", "持有秒數"])
        for trip in trips:
            writer.writerow([
                trip.symbol, "多" if trip.direction > 0 else "空", trip.quantity,
                trip.opened_at.strftime("%H:%M:%S"), trip.entry, trip.closed_at.strftime("%H:%M:%S"), trip.exit,
                round((trip.exit - trip.entry) * trip.direction, 4), round(trip.gross), round(trip.cost),
                round(trip.net), int((trip.closed_at - trip.opened_at).total_seconds()),
            ])


def plot(path: Path, day: str, fills: list[Fill], trips: list[RoundTrip],
         prices: dict[str, list[tuple[datetime, float]]], curve: list[tuple[datetime, float]],
         daily: dict[str, float], initial_equity: float) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = FONTS
    plt.rcParams["axes.unicode_minus"] = False
    symbols = sorted({f.symbol for f in fills} | set(prices))
    rows = len(symbols) + 2
    fig, axes = plt.subplots(rows, 1, figsize=(12, 3.2 * rows), squeeze=False)
    axes = axes[:, 0]
    clock = mdates.DateFormatter("%H:%M", tz=TAIPEI)
    for ax, symbol in zip(axes, symbols):
        series = prices.get(symbol, [])
        if series:
            ax.plot([at for at, _ in series], [p for _, p in series], color="#555555", lw=0.8, label="成交價")
        for trip in (t for t in trips if t.symbol == symbol):
            ax.plot([trip.opened_at, trip.closed_at], [trip.entry, trip.exit],
                    color="#d62728" if trip.net > 0 else "#2ca02c", lw=0.9, alpha=0.7)
        buys = [f for f in fills if f.symbol == symbol and f.side == "buy"]
        sells = [f for f in fills if f.symbol == symbol and f.side == "sell"]
        ax.scatter([f.at for f in buys], [f.price for f in buys], marker="^", color="#d62728", s=28, label="買進", zorder=3)
        ax.scatter([f.at for f in sells], [f.price for f in sells], marker="v", color="#2ca02c", s=28, label="賣出", zorder=3)
        net = sum(t.net for t in trips if t.symbol == symbol)
        ax.set_title(f"{symbol} 價格走勢與沖銷（{sum(t.quantity for t in trips if t.symbol == symbol)} 口沖銷，淨 {net:+,.0f}）")
        ax.xaxis.set_major_formatter(clock)
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.3)
    ax = axes[-2]
    if curve:
        ax.plot([at for at, _ in curve], [v for _, v in curve], color="#1f77b4", lw=1.2)
        ax.axhline(0, color="black", lw=0.6)
        ax.set_title(f"{day} 盤中累積淨損益（收 {curve[-1][1]:+,.0f}）")
    else:
        ax.set_title(f"{day} 沒有盤中資料")
    ax.xaxis.set_major_formatter(clock)
    ax.grid(alpha=0.3)
    ax = axes[-1]
    days = list(daily)
    values = [daily[d] for d in days]
    cumulative = []
    running = 0.0
    for value in values:
        running += value
        cumulative.append(running)
    labels = [d[5:] for d in days]
    ax.bar(labels, values, color=["#d62728" if v >= 0 else "#2ca02c" for v in values])
    ax.axhline(0, color="black", lw=0.6)
    ax.set_ylabel("每日淨損益")
    twin = ax.twinx()
    twin.plot(labels, [100 * c / initial_equity for c in cumulative], color="#ff7f0e", marker="o")
    twin.set_ylabel("累積報酬率 %")
    total = cumulative[-1] if cumulative else 0.0
    ax.set_title(f"競賽以來每日淨損益與累積報酬（累積 {total:+,.0f}，{100 * total / initial_equity:+.2f}%）")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def build_report(*, day: str, db_path: Path, runs_dir: Path, out_dir: Path, costs: Costs | None = None,
                 initial_equity: float = 2_000_000) -> dict[str, Any]:
    """Write the day's chart and CSVs; returns their paths and headline numbers."""
    costs = costs or Costs()
    fills = load_fills(db_path)
    today = [f for f in fills if f.trading_day == day]
    book = Book(costs)
    for fill in fills:
        if fill.trading_day <= day:
            book.apply(fill)
    trips = [t for t in book.closed if t.trading_day == day]
    prices = load_prices(runs_dir, day)
    daily = {d: v for d, v in daily_net(fills, costs).items() if d <= day}
    folder = out_dir / day
    folder.mkdir(parents=True, exist_ok=True)
    paths = {
        "chart": folder / f"report-{day}.png",
        "fills": folder / f"fills-{day}.csv",
        "roundtrips": folder / f"roundtrips-{day}.csv",
    }
    write_fills(paths["fills"], today)
    write_round_trips(paths["roundtrips"], trips)
    plot(paths["chart"], day, today, trips, prices,
         intraday_curve(fills, prices, day, costs), daily, initial_equity)
    wins = sum(1 for t in trips if t.net > 0)
    return {
        "paths": paths,
        "fills": len(today),
        "round_trips": len(trips),
        "wins": wins,
        "net": sum(t.net for t in trips),
        "cumulative": sum(daily.values()),
        "initial_equity": initial_equity,
    }


def report_message(day: str, report: dict[str, Any]) -> str:
    trips = report["round_trips"]
    rate = f"{100 * report['wins'] / trips:.0f}%" if trips else "—"
    cumulative = report["cumulative"]
    return (f"📈 **【收盤圖表】{day}**\n"
            f"成交 {report['fills']} 筆，沖銷 {trips} 筆（勝率 {rate}），以成交估算淨損益 {report['net']:+,.0f}\n"
            f"競賽以來累積 {cumulative:+,.0f}（{100 * cumulative / report['initial_equity']:+.2f}%）\n"
            "附件：圖表、當日成交紀錄、沖銷紀錄（CSV）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the post-close chart and trade records for one day")
    parser.add_argument("--day", default=datetime.now(TAIPEI).date().isoformat())
    parser.add_argument("--db", type=Path, default=Path("output/trades.sqlite3"))
    parser.add_argument("--runs", type=Path, default=Path("output/runs"))
    parser.add_argument("--out", type=Path, default=Path("output/reports"))
    parser.add_argument("--fee-per-fill", type=float, default=50.0)
    parser.add_argument("--tax-rate", type=float, default=0.00002)
    parser.add_argument("--initial-equity", type=float, default=2_000_000)
    args = parser.parse_args(argv)
    report = build_report(day=args.day, db_path=args.db, runs_dir=args.runs, out_dir=args.out,
                          costs=Costs(args.fee_per_fill, args.tax_rate), initial_equity=args.initial_equity)
    print(report_message(args.day, report))
    for path in report["paths"].values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
