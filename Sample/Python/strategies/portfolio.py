from __future__ import annotations

import importlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from strategy_core import FeatureEngine, Fill, OrderIntent, PositionLedger, Quote, StrategyConfig, StrategyDecision

LOGGER = logging.getLogger(__name__)
TAIPEI = timezone(timedelta(hours=8))
REPLACE_ATTEMPTS = 5
MIRROR = "portfolio mirror"


def _seconds(text: str) -> float | None:
    try:
        hh, mm, ss = text.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (AttributeError, ValueError):
        return None


def _load(path: str) -> type:
    module_name, _, class_name = path.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"member module must use module:Class syntax: {path}")
    return getattr(importlib.import_module(module_name), class_name)


def _marketable(intent: OrderIntent, quote: Quote) -> bool:
    return intent.price >= quote.ask if intent.side == "buy" else intent.price <= quote.bid


@dataclass
class KellyStats:
    """Per-contract round-trip results, shrunk toward a backtest prior."""

    prior_n: float
    prior_mean: float
    prior_sd: float
    n: int = 0
    total: float = 0.0
    total_sq: float = 0.0

    def add(self, pnl_per_contract: float) -> None:
        self.n += 1
        self.total += pnl_per_contract
        self.total_sq += pnl_per_contract ** 2

    def mean(self) -> float:
        return (self.prior_n * self.prior_mean + self.total) / (self.prior_n + self.n)

    def variance(self) -> float:
        second = (self.prior_n * (self.prior_sd ** 2 + self.prior_mean ** 2) + self.total_sq) / (self.prior_n + self.n)
        return max(second - self.mean() ** 2, 1e-9)

    def contracts(self, equity: float, fraction: float, cap: int) -> int:
        """Fractional Kelly in whole contracts; a non-positive edge stops the member."""
        mean = self.mean()
        if mean <= 0:
            return 0
        return max(0, min(cap, math.floor(fraction * equity * mean / self.variance())))


@dataclass
class TheoryBook:
    """One member's own ledger, filled by TTB's rule against real prints.

    TTB matches every order independently against the tape ("見價見量"): a
    working buy fills once a print trades at or below its price, for at most
    the volume that printed, and a marketable order fills at the print.
    """

    name: str
    strategy: Any
    ledger: PositionLedger
    stats: KellyStats
    max_qty: int
    qty: int = 1
    working: dict[str, dict[str, Any]] = field(default_factory=dict)
    trade_realized: float = 0.0
    trade_qty: int = 0
    sequence: int = 0

    def committed(self, quote: Quote) -> int:
        """Position plus orders that cross the market and will fill on the next print."""
        position = self.ledger.position
        for side, order in self.working.items():
            if _marketable(order["intent"], quote):
                position += order["remaining"] if side == "buy" else -order["remaining"]
        return position

    def resting(self, side: str, quote: Quote) -> OrderIntent | None:
        order = self.working.get(side)
        if order and not _marketable(order["intent"], quote):
            return replace(order["intent"], quantity=order["remaining"])
        return None

    def match(self, quote: Quote, traded: float) -> list[Fill]:
        fills = []
        if traded <= 0:
            return fills
        for side, order in list(self.working.items()):
            intent: OrderIntent = order["intent"]
            if quote.received_at < order["live_at"]:
                continue
            if side == "buy" and quote.last <= intent.price:
                price = min(intent.price, quote.last)
            elif side == "sell" and quote.last >= intent.price:
                price = max(intent.price, quote.last)
            else:
                continue
            qty = int(min(order["remaining"], traded))
            if qty <= 0:
                continue
            order["remaining"] -= qty
            if order["remaining"] <= 0:
                del self.working[side]
            fills.append(Fill(side, price, qty, order["id"], quote.received_at))
        return fills

    def place(self, intents: tuple[OrderIntent, ...], now: float, latency: float) -> list[tuple[str, OrderIntent]]:
        """Mirror the member's intents as working orders; return what changed."""
        changes = []
        wanted = {intent.side: intent for intent in intents if intent.quantity > 0}
        for side in list(self.working):
            if side not in wanted:
                changes.append(("cancel", self.working.pop(side)["intent"]))
        for side, intent in wanted.items():
            current = self.working.get(side)
            if current and current["intent"] == intent:
                continue
            self.sequence += 1
            self.working[side] = {
                "intent": intent, "live_at": now + latency, "remaining": intent.quantity,
                "id": f"{self.name}-{self.sequence}",
            }
            changes.append(("order", intent))
        return changes


@dataclass(frozen=True)
class PortfolioSettings:
    members: tuple[dict[str, Any], ...]
    equity: float = 2_000_000.0
    kelly_fraction: float = 0.5
    latency_seconds: float = 2.5
    entry_slip_ticks: float = 2.0
    order_ttl_seconds: float = 6.0
    # The engine blocks entries in the first 10 and last 25 minutes; the
    # theory books follow the same session so they stay comparable.
    entry_open_minute: int = 8 * 60 + 55
    entry_cutoff_minute: int = 13 * 60 + 20
    flatten_minute: int = 13 * 60 + 35
    journal_dir: str | None = None

    def validate(self) -> None:
        if not self.members:
            raise ValueError("portfolio needs at least one member")
        names = [member.get("name") for member in self.members]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("every member needs a unique name")
        if self.equity <= 0 or not 0 < self.kelly_fraction <= 1:
            raise ValueError("equity must be positive and kelly_fraction in (0, 1]")
        if self.latency_seconds < 0 or self.entry_slip_ticks < 0 or self.order_ttl_seconds <= 0:
            raise ValueError("invalid order settings")


class PortfolioStrategy:
    """Run several strategies on one contract and trade only their net position.

    Each member keeps its own theoretical ledger, filled by TTB's matching rule
    against the live tape, and every theoretical order and fill is journaled per
    member for later analysis. The real account is driven to the sum of the
    members' positions with marketable orders, so opposite member positions net
    out and never reach TTB. Each member is sized by fractional Kelly from its
    backtest prior plus its own live theoretical results. A member whose
    estimated edge turns non-positive keeps trading one theoretical contract
    so its record stays current, but no longer counts toward the real target.
    """

    def __init__(self, config: StrategyConfig, ledger: PositionLedger, **settings) -> None:
        self.config = config
        self.ledger = ledger
        settings["members"] = tuple(settings.get("members") or ())
        self.settings = PortfolioSettings(**settings)
        self.settings.validate()
        if config.max_inventory > config.max_order_qty:
            # A full flatten goes out as one exit, and the gateway rejects any
            # order above max_order_qty by stopping the bot.
            raise ValueError("portfolio needs max_inventory <= max_order_qty")
        self.features = FeatureEngine(config)
        self.journal_dir = Path(self.settings.journal_dir) if self.settings.journal_dir else None
        self.books: list[TheoryBook] = []
        saved = self._load_stats()
        for spec in self.settings.members:
            prior = spec.get("prior") or {}
            stats = KellyStats(float(prior.get("n", 30)), float(prior.get("mean", 0.0)), float(prior.get("sd", 1.0)))
            stats.n, stats.total, stats.total_sq = saved.get(spec["name"], (0, 0.0, 0.0))
            max_qty = int(spec.get("max_qty", 1))
            member_config = replace(config, base_qty=1, max_order_qty=max(1, max_qty), max_inventory=max(1, max_qty))
            member_ledger = PositionLedger(member_config)
            strategy = _load(spec["module"])(member_config, member_ledger, **(spec.get("params") or {}))
            self.books.append(TheoryBook(spec["name"], strategy, member_ledger, stats, max_qty))
        self.day = ""
        self.previous_volume: float | None = None
        self.real_intent: OrderIntent | None = None
        self.real_intent_at = 0.0
        self.mirror_hit_at = float("-inf")

    def parameters(self) -> dict:
        return {"members": [book.name for book in self.books], "equity": self.settings.equity,
                "kelly_fraction": self.settings.kelly_fraction}

    # -- persistence -----------------------------------------------------
    def _stats_path(self) -> Path | None:
        return self.journal_dir / "kelly-stats.json" if self.journal_dir else None

    def _load_stats(self) -> dict[str, tuple[int, float, float]]:
        path = self._stats_path()
        if not path:
            return {}
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {name: (int(v["n"]), float(v["total"]), float(v["total_sq"])) for name, v in raw.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            LOGGER.warning("portfolio stats %s unreadable, starting from the priors: %s", path, exc)
            return {}

    def _save_stats(self) -> None:
        path = self._stats_path()
        if not path:
            return
        data = {book.name: {"n": book.stats.n, "total": book.stats.total, "total_sq": book.stats.total_sq}
                for book in self.books}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            # A scanner holding the file makes the replace fail on Windows; the
            # stats stay in memory and the next closed trade saves again.
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, path)
                    return
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            LOGGER.warning("portfolio stats %s not saved: %s", path, exc)
            return
        LOGGER.warning("portfolio stats %s locked; kept in memory", path)

    def _journal(self, quote: Quote, member: str, event: str, **values: Any) -> None:
        if not self.journal_dir:
            return
        record = {"wall": datetime.now(TAIPEI).isoformat(timespec="milliseconds"),
                  "exchange_time": quote.exchange_time, "member": member, "event": event, **values}
        try:
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            with (self.journal_dir / f"theory-{self.day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            LOGGER.warning("portfolio journal not written: %s", exc)

    # -- decision --------------------------------------------------------
    def _roll_day(self, quote: Quote) -> None:
        today = datetime.now(TAIPEI).date().isoformat()
        if today == self.day:
            return
        self.day = today
        s = self.settings
        for book in self.books:
            book.qty = book.stats.contracts(s.equity, s.kelly_fraction, book.max_qty)
            book.strategy.config = replace(book.strategy.config, base_qty=max(1, book.qty))
            self._journal(quote, book.name, "sizing", contracts=book.qty, live=book.qty > 0, mean=round(book.stats.mean(), 1),
                          sd=round(book.stats.variance() ** 0.5, 1), live_trades=book.stats.n)

    def _book_fill(self, quote: Quote, book: TheoryBook, fill: Fill) -> None:
        before = book.ledger.position
        if before == 0:
            book.trade_realized = book.ledger.realized_pnl
            book.trade_qty = 0
        book.ledger.on_fill(fill)
        book.trade_qty = max(book.trade_qty, abs(before), abs(book.ledger.position))
        self._journal(quote, book.name, "fill", side=fill.side, price=fill.price, qty=fill.quantity,
                      order_id=fill.order_id, position=book.ledger.position)
        if before != 0 and book.ledger.position == 0:
            pnl = book.ledger.realized_pnl - book.trade_realized
            book.stats.add(pnl / max(1, book.trade_qty))
            self._journal(quote, book.name, "trade_closed", pnl=round(pnl, 1), contracts=book.trade_qty)
            self._save_stats()

    def decide(self, quote: Quote) -> StrategyDecision:
        features = self.features.update(quote)
        if not quote.is_valid() or quote.symbol.upper() != self.config.symbol.upper():
            return StrategyDecision((), features, True, "invalid quote")
        s, now = self.settings, quote.received_at
        self._roll_day(quote)
        traded = 0.0 if self.previous_volume is None else max(0.0, quote.total_volume - self.previous_volume)
        self.previous_volume = quote.total_volume
        seconds = _seconds(quote.exchange_time)
        closing = seconds is not None and seconds >= s.flatten_minute * 60
        no_entry = seconds is None or not s.entry_open_minute * 60 <= seconds < s.entry_cutoff_minute * 60

        for book in self.books:
            for fill in book.match(quote, traded):
                self._book_fill(quote, book, fill)
            if closing:
                # The engine flattens the account now; close the theory the same way.
                book.working.clear()
                if book.ledger.position:
                    pos = book.ledger.position
                    price = quote.bid if pos > 0 else quote.ask
                    self._book_fill(quote, book, Fill("sell" if pos > 0 else "buy", price, abs(pos),
                                                      f"{book.name}-close", now))
                continue
            decision = book.strategy.decide(quote)
            intents = decision.intents
            if no_entry and not book.ledger.position:
                intents = tuple(intent for intent in intents if intent.role != "entry")
            for kind, intent in book.place(intents, now, s.latency_seconds):
                self._journal(quote, book.name, kind, side=intent.side, price=intent.price, qty=intent.quantity,
                              role=intent.role, reason=intent.reason)

        # Marketable member orders count at once, so the real order leaves
        # together with the theoretical one instead of one latency behind it.
        live = [book for book in self.books if book.qty > 0]
        total = sum(book.committed(quote) for book in live)
        target = self._clamp(total)
        real = self.ledger.position
        reason = f"portfolio target={target} real={real} " + " ".join(
            f"{book.name}={book.ledger.position}" for book in self.books)
        # A member crossing the market (a stop, a timed exit) must not wait
        # behind a mirrored resting order.
        crossing = any(book.committed(quote) != book.ledger.position for book in live)
        intent = self._real_intent(target, total, real, crossing, quote)
        return StrategyDecision((intent,) if intent else (), features, False, reason)

    def _clamp(self, position: int) -> int:
        cap = self.config.max_inventory
        return max(-cap, min(cap, position))

    def _real_intent(self, target: int, total: int, real: int, crossing: bool, quote: Quote) -> OrderIntent | None:
        s, tick, now = self.settings, self.config.tick_size, quote.received_at
        current = self.real_intent
        if current and current.reason == MIRROR and (
                quote.last >= current.price if current.side == "sell" else quote.last <= current.price):
            self.mirror_hit_at = now
        if target == real:
            self.real_intent = self._mirror(total, real, quote)
            self.real_intent_at = now
            return self.real_intent
        if real and (target == 0 or (target > 0) != (real > 0) or abs(target) < abs(real)):
            # Reduce first; never cross zero in one order (exits must be reduce-only).
            side = "sell" if real > 0 else "buy"
            qty = abs(real) if target == 0 or (target > 0) != (real > 0) else abs(real) - abs(target)
            role = "exit"
        else:
            side = "buy" if target > real else "sell"
            qty = min(abs(target - real), self.config.max_order_qty)
            role = "entry"
        if current and (current.side, current.quantity, current.role) == (side, qty, role):
            if current.reason == MIRROR:
                # A mirrored exit the tape reached has most likely filled at TTB
                # too; give the fill report one TTL before chasing it.
                if not crossing and now - self.mirror_hit_at < s.order_ttl_seconds:
                    return current
            elif now - self.real_intent_at < s.order_ttl_seconds:
                return current
        # Cross the spread and hold the price steady so the order can reach TTB.
        price = quote.ask + s.entry_slip_ticks * tick if side == "buy" else quote.bid - s.entry_slip_ticks * tick
        self.real_intent = OrderIntent(side, price, qty, role, "portfolio net")
        self.real_intent_at = now
        return self.real_intent

    def _mirror(self, total: int, real: int, quote: Quote) -> OrderIntent | None:
        """Rest a live member's passive exit at TTB too, so both fill on the same print.

        Only the part that would lower the clamped target is mirrored: with the
        account at its cap, a member leaving may not change the net at all.
        """
        if not real:
            return None
        side = "sell" if real > 0 else "buy"
        sign = 1 if real > 0 else -1
        best = None
        for book in self.books:
            order = book.resting(side, quote) if book.qty > 0 else None
            if order is None:
                continue
            after = self._clamp(total - sign * order.quantity)
            qty = min(order.quantity, abs(real) - max(0, sign * after))
            if qty <= 0:
                continue
            if best is None or (order.price < best.price if side == "sell" else order.price > best.price):
                best = OrderIntent(side, order.price, qty, "exit", MIRROR)
        return best
