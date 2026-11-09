"""Deterministic, offline paper venue — the only BrokerAdapter implementation.

This adapter simulates a venue against caller-supplied marks. It never imports
a broker SDK, touches a network, or claims real execution quality. A mark is a
single asserted-executable price per symbol: there is no spread, queue
position, latency, or partial-fill modelling, and an accepted order fills
completely the first time its mark trades through the limit. Fills here are a
simplification — not evidence — of what any real venue would do.

Determinism: identical call sequences produce identical results, including
broker order ids. Time is caller-supplied (`at`); nothing reads a wall clock.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D, ROUND_HALF_EVEN, localcontext, DecimalException
import re

from ..risk import INITIAL_EQUITY
from .base import (BrokerAccountSnapshot, BrokerOrderView, BrokerPosition,
                   CancelAck, DispatchAck, DispatchRequest, FillEvent,
                   SIDES, TIF)

_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_BPS_DENOMINATOR = D(10_000)
_CENT = D("0.01")


def _cents(value):
    if not _money(value, positive=False):
        raise ValueError("INVALID_MONEY")
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            return value.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    except DecimalException as exc:
        raise ValueError("INVALID_MONEY") from exc



def _money(value, *, positive=True):
    return isinstance(value, D) and value.is_finite() and (value > 0 if positive else value >= 0)


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


@dataclass
class _Order:
    """Mutable venue-side record; never exposed directly."""
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: D
    status: str = "ACCEPTED"
    filled: int = 0
    fill_value: D = D(0)

    def view(self):
        avg = _cents(self.fill_value / self.filled) if self.filled else None
        return BrokerOrderView(self.broker_order_id, self.client_order_id,
                               self.symbol, self.side, self.qty,
                               self.limit_price, self.status, self.filled, avg)


class PaperBrokerAdapter:
    """Offline venue simulation with exact cash/share reservation accounting.

    Cash accounting: dispatching a BUY reserves qty*limit + fee; a fill debits
    the actual qty*price + fee; cancel/expiry releases the reservation. A SELL
    reserves shares (long-only: no naked short); a fill credits qty*price - fee
    and reduces cost basis at average cost. Equity is available only if every held position has a mark from the
    latest advance. Missing/stale marks fail closed, never fall back to cost.
    """

    mode = "paper"

    def __init__(self, *, starting_cash: D = INITIAL_EQUITY,
                 slippage_bps: D = D(0), fee_per_order: D = D(0)):
        if (not _money(starting_cash)
                or not _money(slippage_bps, positive=False)
                or slippage_bps >= _BPS_DENOMINATOR
                or not _money(fee_per_order, positive=False)):
            raise ValueError("INVALID_PAPER_VENUE_CONFIG")
        if (starting_cash != _cents(starting_cash)
                or fee_per_order != _cents(fee_per_order)):
            raise ValueError("INVALID_PAPER_VENUE_CONFIG")
        self._cash = _cents(starting_cash)
        self._session_open_equity = self._cash
        self._reserved = D(0)
        self._slippage_bps = slippage_bps
        self._fee = _cents(fee_per_order)
        self._positions = {}        # symbol -> [qty, cost]
        self._reserved_shares = {}  # symbol -> reserved qty for open sells
        self._orders = {}           # broker_order_id -> _Order
        self._by_client = {}        # client_order_id -> broker_order_id
        self._sequence = []         # broker_order_ids in dispatch order
        self._marks = {}            # symbol -> last supplied mark
        self._mark_times = {}       # symbol -> time at which mark was supplied
        self._latest_advance_at = None
        self._events = {}           # broker_order_id -> [FillEvent]

    # ---- venue state ----------------------------------------------------

    def _position(self, symbol):
        return self._positions.get(symbol, [0, D(0)])

    def _mark_value(self):
        for symbol, (qty, _cost) in self._positions.items():
            if qty and (symbol not in self._marks or
                        self._mark_times.get(symbol) != self._latest_advance_at):
                raise ValueError("UNVERIFIED_OR_STALE_MARK")
        return sum(qty * self._marks[sym]
                   for sym, (qty, _cost) in self._positions.items() if qty)

    def account_snapshot(self) -> BrokerAccountSnapshot:
        marked = self._mark_value()
        equity = self._cash + marked
        return BrokerAccountSnapshot(
            mode="paper", equity=equity,
            session_open_equity=self._session_open_equity,
            cash=self._cash, reserved_cash=self._reserved,
            available_cash=self._cash - self._reserved,
            day_pnl=equity - self._session_open_equity)

    def positions(self) -> tuple[BrokerPosition, ...]:
        return tuple(BrokerPosition(sym, qty, _cents(cost / qty))
                     for sym, (qty, cost) in sorted(self._positions.items())
                     if qty)

    def open_orders(self) -> tuple[BrokerOrderView, ...]:
        return tuple(self._orders[ident].view() for ident in self._sequence
                     if self._orders[ident].status in ("ACCEPTED", "PARTIALLY_FILLED"))

    def order_status(self, broker_order_id: str) -> BrokerOrderView | None:
        order = self._orders.get(broker_order_id)
        return order.view() if order else None

    def order_by_client(self, client_order_id: str) -> BrokerOrderView | None:
        """Lookup by client idempotency key; supports lost-ack recovery."""
        broker_id = self._by_client.get(client_order_id)
        return self._orders[broker_id].view() if broker_id else None

    def fills(self, broker_order_id: str) -> tuple[FillEvent, ...]:
        return tuple(self._events.get(broker_order_id, ()))

    # ---- order entry / cancel -------------------------------------------

    def dispatch(self, req: DispatchRequest, *,
                 client_order_id: str) -> DispatchAck:
        """Accept an intent to work at the paper venue or reject it.

        Malformed input fails closed with ValueError; venue-level rejections
        (buying power, shares, client-id conflict) return accepted=False.
        """
        if (not isinstance(req, DispatchRequest)
                or not isinstance(req.symbol, str) or not _SYMBOL.fullmatch(req.symbol)
                or req.side not in SIDES or type(req.qty) is not int
                or not 0 < req.qty <= 1_000_000_000
                or not _money(req.limit_price)
                or req.limit_price != _cents(req.limit_price)
                or req.time_in_force not in TIF
                or not isinstance(client_order_id, str)
                or not _ID.fullmatch(client_order_id)):
            raise ValueError("INVALID_DISPATCH")
        existing_id = self._by_client.get(client_order_id)
        if existing_id is not None:
            order = self._orders[existing_id]
            same = (order.symbol == req.symbol and order.side == req.side
                    and order.qty == req.qty and order.limit_price == req.limit_price)
            if same:
                return DispatchAck(client_order_id, existing_id, True, "DUPLICATE")
            return DispatchAck(client_order_id, None, False,
                               "CLIENT_ORDER_ID_CONFLICT")
        if req.side == "BUY":
            hold = req.qty * req.limit_price + self._fee
            if hold > self._cash - self._reserved:
                return DispatchAck(client_order_id, None, False,
                                   "INSUFFICIENT_CASH")
        else:
            held_qty, _ = self._position(req.symbol)
            if req.qty > held_qty - self._reserved_shares.get(req.symbol, 0):
                return DispatchAck(client_order_id, None, False,
                                   "INSUFFICIENT_SHARES")
        broker_id = f"paper-{len(self._sequence) + 1:08d}"
        order = _Order(broker_id, client_order_id, req.symbol, req.side,
                       req.qty, req.limit_price)
        self._orders[broker_id] = order
        self._by_client[client_order_id] = broker_id
        self._sequence.append(broker_id)
        if req.side == "BUY":
            self._reserved += hold
        else:
            self._reserved_shares[req.symbol] = (
                self._reserved_shares.get(req.symbol, 0) + req.qty)
        return DispatchAck(client_order_id, broker_id, True, "ACCEPTED")

    def _release(self, order):
        if order.side == "BUY":
            self._reserved -= order.qty * order.limit_price + self._fee
        else:
            left = self._reserved_shares.get(order.symbol, 0) - order.qty
            if left:
                self._reserved_shares[order.symbol] = left
            else:
                self._reserved_shares.pop(order.symbol, None)

    def cancel(self, broker_order_id: str) -> CancelAck:
        """Cancel a working order; terminal orders return their status."""
        if not isinstance(broker_order_id, str) or not _ID.fullmatch(broker_order_id):
            raise ValueError("INVALID_BROKER_ORDER_ID")
        order = self._orders.get(broker_order_id)
        if order is None:
            raise ValueError("UNKNOWN_BROKER_ORDER")
        if order.status != "ACCEPTED":
            return CancelAck(broker_order_id, order.status, "ALREADY_TERMINAL")
        self._release(order)
        order.status = "CANCELLED"
        return CancelAck(broker_order_id, "CANCELLED")

    def expire_session(self, *, at) -> tuple[str, ...]:
        """Expire all working orders at caller-asserted session end (DAY only)."""
        if not _aware(at):
            raise ValueError("INVALID_TIMESTAMP")
        expired = []
        for ident in self._sequence:
            order = self._orders[ident]
            if order.status == "ACCEPTED":
                self._release(order)
                order.status = "EXPIRED"
                expired.append(ident)
        return tuple(expired)

    # ---- matching ---------------------------------------------------------

    def advance(self, marks: Mapping[str, D], *, at) -> tuple[FillEvent, ...]:
        """Apply asserted-executable marks; fill working orders deterministically.

        A BUY fills completely iff mark*(1+slippage) <= limit, at that adjusted
        price; a SELL iff mark*(1-slippage) >= limit. One mark fills at most
        once per order — there are no partial fills and no queue priority.
        """
        if not _aware(at):
            raise ValueError("INVALID_TIMESTAMP")
        if not isinstance(marks, Mapping):
            raise ValueError("INVALID_MARKS")
        for symbol, price in marks.items():
            if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                    or not _money(price)):
                raise ValueError("INVALID_MARKS")
        if self._latest_advance_at is not None and at < self._latest_advance_at:
            raise ValueError("NON_MONOTONIC_MARK_TIME")
        clean_marks = {symbol: _cents(price) for symbol, price in marks.items()}
        if any(price <= 0 for price in clean_marks.values()):
            raise ValueError("INVALID_MARKS")
        self._latest_advance_at = at
        self._marks.update(clean_marks)
        self._mark_times.update({symbol: at for symbol in clean_marks})
        marks = clean_marks
        buy_adjust = D(1) + self._slippage_bps / _BPS_DENOMINATOR
        sell_adjust = D(1) - self._slippage_bps / _BPS_DENOMINATOR
        events = []
        for ident in self._sequence:
            order = self._orders[ident]
            if order.status != "ACCEPTED" or order.symbol not in marks:
                continue
            mark = marks[order.symbol]
            if order.side == "BUY":
                price = _cents(mark * buy_adjust)
                if price > order.limit_price:
                    continue
            else:
                price = _cents(mark * sell_adjust)
                if price < order.limit_price:
                    continue
            self._release(order)
            qty, fee = order.qty, self._fee
            if order.side == "BUY":
                self._cash -= qty * price + fee
                held, cost = self._position(order.symbol)
                self._positions[order.symbol] = [held + qty,
                                                 cost + qty * price + fee]
            else:
                self._cash += qty * price - fee
                held, cost = self._position(order.symbol)
                with localcontext() as ctx:
                    ctx.prec = 50
                    cost -= cost if qty == held else _cents(cost * qty / held)
                left = held - qty
                if left:
                    self._positions[order.symbol] = [left, cost]
                else:
                    self._positions.pop(order.symbol, None)
            order.filled = qty
            order.fill_value = qty * price
            order.status = "FILLED"
            event = FillEvent(f"{ident}-fill-1", ident, order.client_order_id,
                              order.symbol, order.side, qty, price, fee, at)
            self._events.setdefault(ident, []).append(event)
            events.append(event)
        return tuple(events)
