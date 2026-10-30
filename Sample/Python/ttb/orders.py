from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from dataclasses import asdict
from typing import Any, Protocol

from strategy_core import Fill, IndependentFillSimulator, OrderIntent, PositionLedger, Quote, StrategyConfig

from .api import TTBApi, TTBApiError, TTBOrderRequest, extract_order_no, is_success_response, position_effect_for
from .risk import DurableOrderRateGuard, PreTradeLimits, PreTradeRiskError, assess_pretrade
from .journal import ExecutionJournal


LOG = logging.getLogger("ttb.orders")
# TTB sleeps 500 ms in every local API request before handling it, so a
# NEWORDER reaches its server at least this long after the bot sends it. An
# order report read no longer ago than this leaves a binding window no wider
# than a fresh snapshot taken right before NEWORDER did. Reuse widens that
# window by at most this much; a late row bound by mistake leaves the real
# order unowned, which latches the bot.
REPORT_REUSE_SECONDS = 0.5
MAX_MARGIN_CACHE_SECONDS = 30.0
# Shutdown flatten IOC prices: ticks through the far touch per attempt. Stock
# futures books are a few lots deep, so an IOC at the touch alone leaves the
# position open. A marketable limit still fills at the best resting prices.
SHUTDOWN_FLATTEN_OFFSET_TICKS = (2, 5, 10)
# Never price a flatten further than this from the touch. TAIFEX daily limits
# are +-10% of the reference price and the touch itself is inside them, so
# only a market already within 3% of a limit can see an attempt rejected. The
# bot cannot tell a rejected NEWORDER from a lost reply, so it never resends
# blindly; the shutdown then fails closed and auto-recovery's market IOC,
# which TAIFEX keeps inside the limits, takes over.
SHUTDOWN_FLATTEN_MAX_OFFSET_FRACTION = 0.03


def shutdown_flatten_price(quote: Quote, side: str, tick_size: float, offset_ticks: int) -> float:
    """Marketable IOC limit price offset_ticks through the far touch, capped by the band above."""
    if tick_size <= 0 or offset_ticks < 0:
        raise ValueError("tick_size must be positive and offset_ticks non-negative")
    touches = [price for price in ((quote.ask, quote.last) if side == "buy" else (quote.bid, quote.last))
               if price > 0]
    if not touches:
        raise ValueError("quote has no usable price for a flatten")
    base = max(touches) if side == "buy" else min(touches)
    # Off-grid touches round away from the book so the price stays marketable.
    base_units = math.ceil(base / tick_size - 1e-9) if side == "buy" else math.floor(base / tick_size + 1e-9)
    cap_units = math.floor(base * SHUTDOWN_FLATTEN_MAX_OFFSET_FRACTION / tick_size + 1e-9)
    units = min(offset_ticks, cap_units)
    if side == "sell":
        units = min(units, base_units - 1)
    return round((base_units + units if side == "buy" else base_units - units) * tick_size, 10)


class ReportChannelLost(RuntimeError):
    """A sent order never reached TTB's order report; only a fresh TTB login restores it."""


def _recursive_json(value: Any) -> Any:
    current = value
    for _ in range(3):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text or text[0] not in "[{\"":
            break
        try:
            current = json.loads(text)
        except json.JSONDecodeError:
            break
    return current


def _response_rows(response: Any) -> list[dict[str, Any]]:
    """Broker reports must be explicit; malformed/missing Data is NOT empty."""
    parsed = _recursive_json(response)
    if not isinstance(parsed, dict) or not is_success_response(parsed):
        raise RuntimeError("TTB report returned an invalid/non-success response")
    if "Data" not in parsed and "data" not in parsed:
        raise RuntimeError("TTB report missing Data; cannot assume an empty account")
    rows = _recursive_json(parsed.get("Data", parsed.get("data")))
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("TTB report has an unsupported Data shape")
    return rows


def _value(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return default


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _side(value: Any) -> str:
    text = str(value).strip().lower()
    return "buy" if text in {"1", "b", "buy", "買", "買別", "買進"} else "sell"


def _explicit_side(value: Any) -> str:
    text = str(value).strip().lower()
    if text in {"1", "b", "buy", "買", "買別", "買進"}:
        return "buy"
    return "sell" if text in {"2", "s", "sell", "賣", "賣別", "賣出"} else ""


def order_number(row: dict[str, Any]) -> str:
    return str(_value(row, "ORDNO", "OrdNo", "OrderNo", "order_no", default=""))


def order_symbol(row: dict[str, Any]) -> str:
    return str(_value(row, "COMD_ID", "ComdID1", "Symbol", default="")).split(".")[0].upper()


def _strict_quantity(value: Any, context: str) -> int:
    try:
        number = float(str(value).replace(",", ""))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"TTB {context} is malformed") from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise RuntimeError(f"TTB {context} is invalid")
    return int(number)


def order_remaining(row: dict[str, Any]) -> int:
    remaining = _value(row, "LESS_VOLM", "RemainQty", "RemainingQty", default=None)
    if remaining is not None:
        return _strict_quantity(remaining, "order remaining quantity")
    ordered = _value(row, "VOLM", "OrderQty", default=None)
    filled = _value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=None)
    if ordered is not None and filled is not None:
        total = _strict_quantity(ordered, "order quantity")
        deal = _strict_quantity(filled, "filled quantity")
        if deal > total:
            raise RuntimeError("TTB filled quantity exceeds order quantity")
        return total - deal
    status = str(_value(row, "STAT", "Status", default="")).strip().lower()
    if any(token in status for token in ("刪", "取消", "完全成交", "全部成交", "cancel", "filled", "rejected", "拒絕")):
        return 0
    raise RuntimeError("TTB cannot determine order remaining quantity")


def is_terminal_order(row: dict[str, Any]) -> bool:
    status = str(_value(row, "STAT", "Status", default="")).strip().lower()
    terminal = ("刪", "取消", "完全成交", "全部成交", "cancel", "filled", "rejected", "拒絕")
    return any(token in status for token in terminal) or (
        order_remaining(row) == 0
        and int(_float(_value(row, "VOLM", "OrderQty", default=0))) > 0
    )


def is_working_order(row: dict[str, Any]) -> bool:
    return bool(order_number(row)) and order_remaining(row) > 0 and not is_terminal_order(row)


def find_sent_order(
    rows: list[dict[str, Any]], known: set[str], symbol: str, side: str, price: float, quantity: int,
) -> str:
    """Return the OrdNo of the one new report row matching an order just sent.

    TTB's NEWORDER reply never carries OrdNo; the number only arrives in the
    order report. Binding by side/price/quantity is sound only because the
    account is exclusive to this bot, and only while one candidate exists.
    """
    target = symbol.split(".")[0].upper()
    matches = {
        order_number(row) for row in rows
        if order_number(row) and order_number(row) not in known
        and order_symbol(row) in ("", target)
        and _explicit_side(_value(row, "BS", "Side", "ComdBS1")) == side
        and abs(_float(_value(row, "ORDR_PRCE", "Price", default=0)) - price) < 1e-6
        and int(_float(_value(row, "VOLM", "OrderQty", default=0))) == quantity
    }
    if len(matches) > 1:
        raise RuntimeError("Several new TTB orders match one sent order; reconcile manually: "
                           + ", ".join(sorted(matches)))
    return next(iter(matches), "")


def reported_position(response: Any, symbol: str) -> int:
    """Return signed open quantity for symbol from TTB's inventory report."""
    target = symbol.split(".")[0].upper()
    total = 0
    for row in _response_rows(response):
        raw_hold = _value(row, "Hold", "HOLD", "PositionQty", "Qty", default=None)
        if raw_hold is None:
            raise RuntimeError("TTB position report missing Hold/quantity")
        hold = _strict_quantity(raw_hold, "position quantity")
        if not hold:
            continue
        if not any(_value(row, *keys, default="") for keys in
                   (("ComdID1", "COMD_ID", "Symbol"), ("ComdID2",))):
            raise RuntimeError("TTB position report contains a position with no symbol")
        for symbol_keys, side_keys in (
            (("ComdID1", "COMD_ID", "Symbol"), ("ComdBS1", "BS", "Side")),
            (("ComdID2",), ("ComdBS2",)),
        ):
            leg_symbol = str(_value(row, *symbol_keys, default="")).split(".")[0].upper()
            if not leg_symbol or leg_symbol != target:
                continue
            side_value = str(_value(row, *side_keys, default="")).strip().lower()
            if side_value not in {"1", "b", "buy", "買", "買別", "買進", "2", "s", "sell", "賣", "賣出"}:
                raise RuntimeError("TTB position report has an unknown side")
            side = _side(side_value)
            total += hold if side == "buy" else -hold
    return total


class OrderGateway(Protocol):
    ledger: PositionLedger

    def before_decision(self, quote: Quote) -> list[Fill]: ...

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


class ShadowGateway:
    """Order gateway that records simulated fills and never calls TTB orders."""

    def __init__(self, ledger: PositionLedger, simulator: IndependentFillSimulator | None = None) -> None:
        self.ledger = ledger
        self.simulator = simulator or IndependentFillSimulator()

    def before_decision(self, quote: Quote) -> list[Fill]:
        fills = self.simulator.on_quote(quote)
        for fill in fills:
            delta = self.ledger.on_fill(fill)
            LOG.info(
                "SIM FILL %s %s @ %.2f id=%s pnl_delta=%.2f position=%s realized=%.2f",
                fill.side,
                fill.quantity,
                fill.price,
                fill.order_id,
                delta,
                self.ledger.position,
                self.ledger.realized_pnl,
            )
        return fills

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None:
        self.simulator.sync(intents, quote.received_at, quote)

    def snapshot(self) -> dict[str, Any]:
        return {
            side: {"price": order.intent.price, "qty": order.remaining, "role": order.intent.role}
            for side, order in self.simulator.orders.items()
        }


class TTBOrderGateway:
    """Fail-closed TTB order gateway with one working order per side."""

    def __init__(
        self,
        module_or_api: Any,
        config: StrategyConfig,
        ledger: PositionLedger,
        poll_seconds: float,
        risk_limits: PreTradeLimits | None = None,
        risk_counters_path: Path | None = None,
        journal_dir: Path | None = None,
        margin_cache_seconds: float = 5.0,
    ) -> None:
        self.api = module_or_api if isinstance(module_or_api, TTBApi) else TTBApi(module_or_api)
        # Keep the old public attribute for code that inspected it.
        self.module = self.api.module
        self.config = config
        self.ledger = ledger
        self.poll_seconds = max(0.25, poll_seconds)
        self.active: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.owned_order_nos: set[str] = set()
        self.preexisting_order_nos: set[str] = set()
        self.seen_deal_qty: dict[str, int] = {}
        self.last_poll = 0.0
        self.bootstrapped = False
        self.cancel_requested: set[str] = set()
        self.reported_position = 0
        self.position_mismatch_polls = 0
        self.max_position_mismatch_polls = 3
        self.risk_limits = risk_limits or PreTradeLimits()
        self.order_rate = DurableOrderRateGuard(
            risk_counters_path or Path("output") / f"rate-counters-{config.symbol.upper()}.json",
            config.symbol,
            self.risk_limits,
        )
        self.journal = ExecutionJournal(
            journal_dir or ((risk_counters_path.parent / "journal") if risk_counters_path
                            else Path("output") / "journal"),
            config.symbol,
        )
        self.current_trading_day = ""
        self._last_broker_report: dict[str, Any] | None = None
        # Order ownership must be proven by an API-returned order ID; matching
        # side/price/quantity cannot distinguish a bot order from a manual one.
        self.unknown_order_nos: set[str] = set()
        self.unsafe_reason = ""
        self.risk_block_reason = ""
        # Each TTB call costs 0.5 s or more; reuse recent reads in _place.
        if not 0.0 <= margin_cache_seconds <= MAX_MARGIN_CACHE_SECONDS:
            raise ValueError(f"margin_cache_seconds must be within 0..{MAX_MARGIN_CACHE_SECONDS:g}")
        self.margin_cache_seconds = margin_cache_seconds
        self._margin_cache: tuple[float, int, int, Any] | None = None
        self._report_order_nos: set[str] = set()
        self._report_read_at = float("-inf")

    def set_trading_day(self, trading_day: str) -> None:
        if self.current_trading_day != trading_day:
            self.current_trading_day = trading_day
            self._margin_cache = None
            self._report_read_at = float("-inf")
            _, cursors = self.journal.replay_ledger(trading_day, self.config)
            self.seen_deal_qty = cursors

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        self.journal.append(kind, payload, self.current_trading_day or None)

    def _assert_safe(self) -> None:
        if self.unsafe_reason:
            raise RuntimeError(self.unsafe_reason)

    def _latch_unsafe(self, reason: str, order_nos: set[str] | None = None) -> None:
        if order_nos:
            self.unknown_order_nos.update(order_nos)
        if not self.unsafe_reason:
            self.unsafe_reason = reason
        LOG.critical("TTB order safety latch: %s", self.unsafe_reason)
        raise RuntimeError(self.unsafe_reason)

    def before_decision(self, quote: Quote) -> list[Fill]:
        self._assert_safe()
        if time.monotonic() - self.last_poll < self.poll_seconds:
            return []
        self.last_poll = time.monotonic()
        # Positions first, so the order report is the freshest read of the
        # cycle and an order placed right after this poll can bind against it.
        position_response = self.api.query_positions()
        if not is_success_response(position_response):
            raise RuntimeError("Position reconciliation failed")
        response = self.api.query_orders()
        if not is_success_response(response):
            raise RuntimeError("Order reconciliation failed")
        read_at = time.monotonic()
        self.reported_position = reported_position(position_response, self.config.symbol)
        rows = _response_rows(response)
        self._remember_report(rows, read_at)
        report_summary = {
            "orders": [{
                "order_no": order_number(row),
                "symbol": order_symbol(row),
                "remaining": order_remaining(row),
                "filled": int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0))),
            } for row in rows],
            "reported_position": self.reported_position,
        }
        if report_summary != self._last_broker_report:
            self.record_event("broker_report", report_summary)
            self._last_broker_report = report_summary
        if not self.bootstrapped:
            self.preexisting_order_nos = {
                order_number(row)
                for row in rows
                if order_number(row)
            }
            # A one-symbol bot must not start when another contract has
            # unowned account exposure (including the old expiry month).
            # Otherwise a manual symbol edit could effectively cross-roll an
            # unresolved position without reconciliation.
            working = [
                order_number(row) or "UNKNOWN"
                for row in rows if is_working_order(row)
            ]
            if working:
                raise RuntimeError(
                    "Working orders already exist in TTB account; reconcile manually before startup: "
                    + ", ".join(working)
                )
            open_positions = [
                row for row in _response_rows(position_response)
                if _strict_quantity(
                    _value(row, "Hold", "HOLD", "PositionQty", "Qty", default=None),
                    "position quantity",
                ) > 0
            ]
            if open_positions:
                raise RuntimeError(
                    "Non-flat TTB account position exists; reconcile ALL symbols before startup"
                )
            self.bootstrapped = True
            LOG.info(
                "TTB baseline loaded: %s existing order ids ignored; position confirmed flat",
                len(self.preexisting_order_nos),
            )
            return []
        self._bind_sent_orders(rows)
        # Check the entire report before applying any fills or permitting a
        # new strategy decision; rows bound above are owned, the rest are not.
        unknown_orders: set[str] = set()
        working_by_side: dict[str, set[str]] = {"buy": set(), "sell": set()}
        for row in rows:
            symbol = order_symbol(row)
            if symbol and symbol != self.config.symbol.upper():
                continue
            number = order_number(row)
            working = order_remaining(row) > 0 and not is_terminal_order(row)
            if working and not number:
                self._latch_unsafe("TTB working order has no order number; reconcile manually")
            if not number:
                continue
            if number not in self.owned_order_nos:
                # Historical terminal orders were present at startup; they are
                # not evidence of an unexpected live execution.
                if working or (
                    number not in self.preexisting_order_nos
                    and int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0))) > 0
                ):
                    unknown_orders.add(number)
            elif working:
                side = _side(_value(row, "BS", "Side", "ComdBS1"))
                working_by_side[side].add(number)
        if unknown_orders:
            self._latch_unsafe(
                "Unowned TTB working/filled order(s); reconcile manually: "
                + ", ".join(sorted(unknown_orders)),
                unknown_orders,
            )
        for side, numbers in working_by_side.items():
            if len(numbers) > 1:
                self._latch_unsafe(
                    f"Multiple bot-owned working orders on {side}; reconcile manually: "
                    + ", ".join(sorted(numbers))
                )

        fills: list[Fill] = []
        for row in rows:
            symbol = order_symbol(row)
            if symbol and symbol != self.config.symbol.upper():
                continue
            order_no = order_number(row)
            if not order_no:
                continue
            side = _side(_value(row, "BS", "Side", "ComdBS1"))
            price = _float(_value(row, "DEAL_PRCE", "DealPrice", default=0))
            ordered_price = _float(_value(row, "ORDR_PRCE", "Price", default=0))
            ordered_qty = int(_float(_value(row, "VOLM", "OrderQty", default=0)))
            if order_no not in self.owned_order_nos:
                # Other terminal orders are not ours; never infer ownership
                # from a matching pending intent.
                continue
            deal_total = int(_float(_value(row, "DEAL_TOTL", "DealQty", "FilledQty", default=0)))
            previous = self.seen_deal_qty.get(order_no, 0)
            if deal_total > previous:
                if price <= 0:
                    raise RuntimeError("TTB fill price invalid; manual reconciliation required")
                fill = Fill(side, price, deal_total - previous, order_no, quote.received_at)
                # Durable append precedes the in-memory ledger mutation.
                self.record_event("fill", {
                    "order_id": order_no, "side": side, "price": price,
                    "quantity": fill.quantity, "deal_total": deal_total,
                })
                self.ledger.on_fill(fill)
                fills.append(fill)
            self.seen_deal_qty[order_no] = max(previous, deal_total)
            remaining = order_remaining(row)
            if remaining > 0 and not is_terminal_order(row):
                known = self.active.get(side, {})
                self.active[side] = {
                    "order_no": order_no, "price": ordered_price, "qty": remaining,
                    "effect": known.get("effect") if known.get("order_no") == order_no else None,
                }
            elif self.active.get(side, {}).get("order_no") == order_no:
                self.active.pop(side, None)
                self.cancel_requested.discard(order_no)

        if self.reported_position != self.ledger.position:
            self.position_mismatch_polls += 1
            LOG.warning(
                "TTB position mismatch poll=%s/%s reported=%s ledger=%s",
                self.position_mismatch_polls,
                self.max_position_mismatch_polls,
                self.reported_position,
                self.ledger.position,
            )
            if self.position_mismatch_polls >= self.max_position_mismatch_polls:
                raise RuntimeError(
                    "TTB position mismatch persisted; reconcile manually "
                    f"(reported={self.reported_position}, ledger={self.ledger.position})"
                )
        else:
            self.position_mismatch_polls = 0
        return fills

    def sync(self, intents: tuple[OrderIntent, ...], quote: Quote) -> None:
        self._assert_safe()
        if not self.bootstrapped:
            return
        if any(time.monotonic() - pending["created_at"] > 10 for pending in self.pending.values()):
            # TTB's report socket can die silently; a restart re-reads the
            # day's orders from the server before recovery cancels them. Latch
            # so a shutdown flatten never trades around the unknown order.
            reason = "Sent order never appeared in the TTB order report; report channel presumed lost"
            self.unsafe_reason = self.unsafe_reason or reason
            raise ReportChannelLost(reason)
        desired = {intent.side: intent for intent in intents if intent.quantity > 0}
        # Wait for every obsolete order's terminal report before sending a
        # replacement.  This prevents duplicate working orders after latency.
        for side, current in list(self.active.items()):
            wanted = desired.get(side)
            if wanted is None or self._stale(current, wanted):
                self._cancel(side, current)
        if self.cancel_requested or self.pending:
            return
        for side in ("buy", "sell"):
            current = self.active.get(side)
            wanted = desired.get(side)
            if current and wanted is None:
                self._cancel(side, current)
            elif current and wanted and self._stale(current, wanted):
                self._cancel(side, current)
            elif wanted and not current and side not in self.pending:
                self._place(wanted)

    def _stale(self, current: dict[str, Any], wanted: OrderIntent) -> bool:
        # A fill on the other side can turn an opening order into a closing
        # one (or back); TTB fixes PositionEffect at entry, so replace it.
        return (
            current["price"] != wanted.price
            or current["qty"] != wanted.quantity
            or current.get("effect") != position_effect_for(wanted.side, wanted.quantity, self.ledger.position)
        )

    def _place(self, intent: OrderIntent) -> None:
        now = time.time()
        try:
            assess_pretrade(
                intent, self.ledger.position, self.active, self.config,
                self.risk_limits, order_rate=self.order_rate, now=now,
                margin_supplier=self._margin_response,
            )
        except PreTradeRiskError as exc:
            self.risk_block_reason = str(exc)
            # A crash in audit persistence itself also blocks the order.
            self.record_event("risk_block", {
                "reason": self.risk_block_reason,
                "role": intent.role,
                "side": intent.side,
                "quantity": intent.quantity,
            })
            raise
        self.risk_block_reason = ""
        # Persist the attempt before the network side effect; do not allow a
        # process restart to reset the rate budget or replay a failed order.
        self.order_rate.record("order", now)
        self.record_event("order_intent", {
            "side": intent.side, "quantity": intent.quantity,
            "price": intent.price, "role": intent.role,
        })
        effect = position_effect_for(intent.side, intent.quantity, self.ledger.position)
        request = TTBOrderRequest(
            symbol=self.config.symbol,
            side=intent.side,
            price=intent.price,
            quantity=intent.quantity,
            time_in_force="2" if intent.role == "risk_exit" else "1",
            position_effect=effect,
        )
        known = self._report_snapshot()
        response = self.api.new_order(request)
        self._track_sent(intent, effect, extract_order_no(response), known)
        LOG.info(
            "TTB NEW %s qty=%s price=%s role=%s response=%s",
            intent.side,
            intent.quantity,
            intent.price,
            intent.role,
            response,
        )

    def _margin_response(self) -> Any:
        """QUERYMARGIN, reused for a few seconds while neither the position nor exposure has grown.

        TTB makes four remote calls per QUERYMARGIN (about 0.7 s). It is not
        known whether TTB reserves margin for working orders, so a read vouches
        only for what it can have seen either way: the absolute position (a
        fill that grows it lowers headroom even when exposure stays put) and
        the worst one-sided exposure of the position plus bound working
        orders, as in validate_exposure. Unbound orders may not have reached
        TTB when it answered, so they count against the current exposure but
        never towards the cached one. Floating PnL within the window is not
        reflected; margin_cache_seconds bounds that.
        """
        now = time.monotonic()
        position = self.ledger.position

        def exposure(include_pending: bool) -> int:
            working = [(side, int(order.get("qty", 0))) for side, order in self.active.items()]
            if include_pending:
                working += [(side, int(sent["intent"].quantity)) for side, sent in self.pending.items()]
            buys = sum(qty for side, qty in working if side == "buy")
            sells = sum(qty for side, qty in working if side == "sell")
            return max(abs(position + buys), abs(position - sells))

        if self._margin_cache is not None:
            read_at, read_held, read_exposure, response = self._margin_cache
            if (now - read_at < self.margin_cache_seconds and abs(position) <= read_held
                    and exposure(True) <= read_exposure):
                return response
        response = self.api.query_margin()
        self._margin_cache = (
            (now, abs(position), exposure(False), response) if is_success_response(response) else None
        )
        return response

    def _remember_report(self, rows: list[dict[str, Any]], read_at: float) -> None:
        self._report_order_nos = {order_number(row) for row in rows if order_number(row)}
        self._report_read_at = read_at

    def _report_snapshot(self, fresh: bool = False) -> set[str]:
        # Every OrdNo already in the report before NEWORDER can never be the
        # new order, even a same-price row that arrived after the last poll.
        # A poll read within REPORT_REUSE_SECONDS stands in for a fresh read.
        if not fresh and time.monotonic() - self._report_read_at <= REPORT_REUSE_SECONDS:
            return set(self._report_order_nos)
        rows = _response_rows(self.api.query_orders())
        self._remember_report(rows, time.monotonic())
        return set(self._report_order_nos)

    def _track_sent(self, intent: OrderIntent, effect: str, order_no: str, known: set[str]) -> None:
        if order_no:
            self._own(intent, effect, order_no, "response")
        else:
            self.pending[intent.side] = {
                "intent": intent, "effect": effect, "known": known, "created_at": time.monotonic(),
            }

    def _own(self, intent: OrderIntent, effect: str, order_no: str, source: str) -> None:
        self.record_event("order_ack", {
            "order_no": order_no, "side": intent.side, "quantity": intent.quantity,
            "price": intent.price, "role": intent.role, "source": source,
        })
        self.owned_order_nos.add(order_no)
        self.active[intent.side] = {
            "order_no": order_no, "price": intent.price, "qty": intent.quantity, "effect": effect,
        }

    def _bind_sent_orders(self, rows: list[dict[str, Any]]) -> None:
        for side, sent in list(self.pending.items()):
            intent = sent["intent"]
            known = sent["known"] | self.owned_order_nos
            order_no = find_sent_order(rows, known, self.config.symbol, side, intent.price, intent.quantity)
            if order_no:
                del self.pending[side]
                self._own(intent, sent["effect"], order_no, "report")

    def _cancel(self, side: str, current: dict[str, Any]) -> None:
        order_no = current["order_no"]
        if order_no in self.cancel_requested:
            return
        counter_failure = None
        try:
            self.order_rate.record("cancel", time.time())
        except Exception as exc:
            counter_failure = exc
            LOG.critical("unable to persist cancel attempt; still attempting safety cancel: %s", exc)
        self.record_event("cancel_intent", {"side": side, "order_no": order_no})
        try:
            self.api.cancel_order(order_no)
        except TTBApiError:
            # TTB rejects cancelling an order that filled between report
            # polls; the next poll books that fill. Anything else stays fatal.
            if not self._reported_terminal(order_no):
                raise
            self.record_event("cancel_rejected_terminal", {"side": side, "order_no": order_no})
            LOG.warning("TTB CANCEL %s %s rejected; order already terminal in the report", side, order_no)
        else:
            self.record_event("cancel_ack", {"side": side, "order_no": order_no})
            LOG.info("TTB CANCEL %s %s", side, order_no)
        self.cancel_requested.add(order_no)
        if counter_failure:
            self._latch_unsafe("rate-counter persistence failed during cancel; manual reconciliation required")

    def _reported_terminal(self, order_no: str) -> bool:
        response = self.api.query_orders()
        if not is_success_response(response):
            return False
        rows = [row for row in _response_rows(response) if order_number(row) == order_no]
        return bool(rows) and all(is_terminal_order(row) and order_remaining(row) == 0 for row in rows)

    def cancel_all(self) -> None:
        for side, current in list(self.active.items()):
            try:
                self._cancel(side, current)
            except Exception:
                LOG.exception("unable to cancel %s", current)

    def emergency_flatten(self, quote: Quote, timeout_seconds: float = 45.0) -> bool:
        """Best-effort IOC flatten for an explicitly requested graceful shutdown.

        The broker-reported position is authoritative. Existing working orders
        are cancelled first; a new opposite-side IOC order is only sent after
        the order report confirms no working order remains for the symbol.
        An IOC that leaves lots open is followed by a more aggressive one for
        the rest (SHUTDOWN_FLATTEN_OFFSET_TICKS), each only once the broker
        position and the journaled fills agree.
        """
        if not self.bootstrapped or not quote.is_valid() or self.unsafe_reason or self.pending:
            return False
        self.cancel_all()
        deadline = time.monotonic() + max(2.0, timeout_seconds)
        attempts = 0
        while time.monotonic() < deadline:
            orders_response = self.api.query_orders()
            position_response = self.api.query_positions()
            if not is_success_response(orders_response) or not is_success_response(position_response):
                time.sleep(self.poll_seconds)
                continue
            rows = _response_rows(orders_response)
            working = [
                order_number(row)
                for row in rows
                if (not order_symbol(row) or order_symbol(row) == self.config.symbol.upper())
                and is_working_order(row)
            ]
            self.reported_position = reported_position(position_response, self.config.symbol)
            if self.reported_position == 0:
                # Broker position can update before the filled order appears in
                # the order report. Keep polling until the local ledger sees the
                # execution too; never send a second flatten while broker is flat.
                self._flatten_poll(quote)
                if self.ledger.position == 0:
                    return True
                time.sleep(self.poll_seconds)
                continue
            if working:
                time.sleep(self.poll_seconds)
                continue
            if attempts < len(SHUTDOWN_FLATTEN_OFFSET_TICKS) and not self.pending:
                if attempts:
                    # The rest is known only from one read in which the previous
                    # IOC is bound and terminal, its final fills are journaled
                    # and the broker position agrees with them.
                    self._flatten_poll(quote)
                if not attempts or (
                    not self.pending and not self.active
                    and self.reported_position == self.ledger.position != 0
                ):
                    self._send_shutdown_flatten(quote, SHUTDOWN_FLATTEN_OFFSET_TICKS[attempts])
                    attempts += 1
            self._flatten_poll(quote)
            time.sleep(self.poll_seconds)
        return False

    def _flatten_poll(self, quote: Quote) -> None:
        try:
            self.last_poll = 0.0
            self.before_decision(quote)
        except RuntimeError as exc:
            # Position mismatch is expected briefly while broker reports
            # transition; any other reconciliation failure remains fatal.
            if "position mismatch" not in str(exc).lower():
                raise

    def _send_shutdown_flatten(self, quote: Quote, offset_ticks: int) -> None:
        side = "sell" if self.reported_position > 0 else "buy"
        quantity = abs(self.reported_position)
        price = shutdown_flatten_price(quote, side, self.config.tick_size, offset_ticks)
        self.order_rate.record("order", time.time())
        self.record_event("order_intent", {
            "side": side, "quantity": quantity,
            "role": "shutdown_flatten", "price": price,
        })
        # A shutdown flatten is rare; never bind it against a reused read.
        known = self._report_snapshot(fresh=True)
        response = self.api.new_order(TTBOrderRequest(
            symbol=self.config.symbol, side=side, price=price, quantity=quantity,
            time_in_force="2", position_effect="Y",
        ))
        self._track_sent(
            OrderIntent(side, price, quantity, "risk_exit", "shutdown flatten"),
            "Y", extract_order_no(response), known,
        )
        LOG.warning("shutdown flatten sent side=%s qty=%s price=%s offset_ticks=%s",
                    side, quantity, price, offset_ticks)

    def finalize_shutdown(self, timeout_seconds: float = 10.0) -> bool:
        """Cancel tracked orders and verify that the TTB account is flat before exit."""
        self.cancel_all()
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            orders_response = self.api.query_orders()
            position_response = self.api.query_positions()
            if not is_success_response(orders_response) or not is_success_response(position_response):
                time.sleep(self.poll_seconds)
                continue
            rows = _response_rows(orders_response)
            self._bind_sent_orders(rows)
            self.cancel_all()
            working = [
                order_number(row)
                for row in rows
                if (not order_symbol(row) or order_symbol(row) == self.config.symbol.upper())
                and is_working_order(row)
            ]
            self.reported_position = reported_position(position_response, self.config.symbol)
            if not working and not self.pending and self.reported_position == 0 and self.ledger.position == 0:
                self.active.clear()
                self.cancel_requested.clear()
                return True
            time.sleep(self.poll_seconds)
        LOG.error(
            "shutdown reconciliation failed pending=%s reported_position=%s ledger_position=%s",
            list(self.pending),
            self.reported_position,
            self.ledger.position,
        )
        return False

    def snapshot(self) -> dict[str, Any]:
        try:
            rate_counters = self.order_rate.snapshot(time.time())
        except Exception as exc:
            # An error-reporting path must never raise a second exception and
            # prevent BotEngine from setting its fail-closed stop_event.
            rate_counters = {"error": str(exc)}
        return {
            "active": self.active,
            "pending": {side: asdict(value["intent"]) for side, value in self.pending.items()},
            "unknown_orders": sorted(self.unknown_order_nos),
            "unsafe_reason": self.unsafe_reason,
            "risk_block_reason": self.risk_block_reason,
            "reported_position": self.reported_position,
            "position_mismatch_polls": self.position_mismatch_polls,
            "rate_counters": rate_counters,
        }
