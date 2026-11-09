"""Venue-facing types and the adapter protocol for the execution layer.

This is the *only* interface through which order intents may reach a venue.
The protocol is deliberately small and free of broker SDK vocabulary (see
docs/BROKER_ADAPTER.md): `dispatch` and `cancel` are the only calls that can
change venue exposure; every other method reads asserted venue state. The
adapter makes no risk decisions — the caller must re-run `validate_buy`
immediately before dispatch. A live implementation is a separate,
tripwire-gated authorization and must never land here unmodified.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D
from typing import Protocol

SIDES = frozenset({"BUY", "SELL"})
ORDER_TYPES = frozenset({"LIMIT"})
TIF = frozenset({"DAY"})
# Venue-side vocabulary: ACCEPTED means working at the venue. Ledger-internal
# states (CREATED, SENT, CANCEL_PENDING) never appear here — see orders.py.
VENUE_OPEN = frozenset({"ACCEPTED", "PARTIALLY_FILLED"})
VENUE_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})
VENUE_STATUSES = VENUE_OPEN | VENUE_TERMINAL


@dataclass(frozen=True)
class DispatchRequest:
    """An order intent handed to the venue; not yet a venue order."""
    symbol: str
    side: str
    qty: int
    limit_price: D
    time_in_force: str = "DAY"


@dataclass(frozen=True)
class DispatchAck:
    """Venue's synchronous answer to a dispatch attempt."""
    client_order_id: str
    broker_order_id: str | None
    accepted: bool
    reason: str


@dataclass(frozen=True)
class CancelAck:
    """Venue's answer to a cancel request for a working order."""
    broker_order_id: str
    status: str          # venue status after the request
    reason: str = ""


@dataclass(frozen=True)
class BrokerOrderView:
    """The venue's view of one order, as asserted by the adapter."""
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: D
    status: str
    filled: int
    avg_fill_price: D | None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int
    avg_price: D          # cost basis, not a market mark


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    """Venue-asserted account truth for reconciliation; caller marks equity."""
    mode: str
    equity: D
    session_open_equity: D
    cash: D
    reserved_cash: D
    available_cash: D
    day_pnl: D


@dataclass(frozen=True)
class FillEvent:
    """A venue-asserted execution; price realism is the adapter's contract."""
    fill_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int
    price: D
    fee: D
    occurred_at: datetime


class BrokerAdapter(Protocol):
    """Minimal venue contract. `mode` is fixed at construction ("paper")."""

    mode: str

    def account_snapshot(self) -> BrokerAccountSnapshot: ...
    def positions(self) -> tuple[BrokerPosition, ...]: ...
    def open_orders(self) -> tuple[BrokerOrderView, ...]: ...
    def order_status(self, broker_order_id: str) -> BrokerOrderView | None: ...
    def order_by_client(self, client_order_id: str) -> BrokerOrderView | None: ...
    def fills(self, broker_order_id: str) -> tuple[FillEvent, ...]: ...
    def dispatch(self, req: DispatchRequest, *,
                 client_order_id: str) -> DispatchAck: ...
    def cancel(self, broker_order_id: str) -> CancelAck: ...
