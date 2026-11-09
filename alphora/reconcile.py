"""Compare internal book/ledger state against asserted venue truth.

Reconciliation answers one question: does the durable internal record agree
with what the venue claims is true? Severity is fail-closed:

- FATAL — truths diverged (position/cash mismatch, unknown venue orders or
  fills, orders the venue lost, stale open venue orders). Requires operator
  attention and blocks new dispatch; no silent repair.
- WARN — explainable in-flight or transient state (undelivered intents, lost
  acks, cancels in flight, unsettled fills, tiny cash drift). The order
  manager's sync() heals these; they still block new dispatch until a CLEAN
  certificate is produced.
- CLEAN — positions exact, cash within epsilon, order sets agree, fills match.

The returned Reconciliation is an *internal capability token*: dispatch paths
require a fresh CLEAN one so "reconciled" is a produced artifact, not a
caller-asserted boolean. It is not authentication — a caller can construct
the dataclass directly; the point is that honest callers must produce one.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D

from .broker.base import BrokerAdapter, VENUE_TERMINAL
from .orders import OrderLedger
from .reservations import OfflineReservations

CLEAN, WARN, FATAL = "CLEAN", "WARN", "FATAL"
_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})


@dataclass(frozen=True)
class Diff:
    severity: str          # WARN or FATAL
    kind: str
    detail: str


@dataclass(frozen=True)
class Reconciliation:
    """Certified result of one reconcile() pass; freshness is the caller's."""
    status: str
    session_id: str
    checked_at: datetime
    diffs: tuple[Diff, ...]
    book_cash: D
    venue_cash: D
    open_order_count: int


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def reconcile(book: OfflineReservations, ledger: OrderLedger,
              adapter: BrokerAdapter, *, at,
              cash_epsilon: D = D("0.01")) -> Reconciliation:
    """Diff book/ledger vs venue truth into a Reconciliation certificate."""
    if (not isinstance(book, OfflineReservations)
            or not isinstance(ledger, OrderLedger)
            or not hasattr(adapter, "open_orders")
            or not _aware(at)
            or not isinstance(cash_epsilon, D)
            or not cash_epsilon.is_finite() or cash_epsilon < 0):
        raise ValueError("INVALID_RECONCILE_INPUT")
    if ledger.session_id != book.snapshot().session_id:
        raise ValueError("SESSION_MISMATCH")

    diffs = []
    book_snap = book.snapshot()
    venue_snap = adapter.account_snapshot()
    venue_positions = {p.symbol: p for p in adapter.positions()}
    ledger_orders = {o.client_order_id: o for o in ledger.orders()}
    ledger_open = {o.client_order_id: o for o in ledger.open_orders()}
    venue_open = {o.broker_order_id: o for o in adapter.open_orders()}

    # --- expected book state: raw state plus fills the venue has asserted -
    # but the ledger/book have not yet applied. Venue truth is compared
    # against *expected* for FATAL divergence, and against raw book for WARN
    # settlement lag. Anything else is unexplained divergence.
    book_positions = {sym: qty for sym, qty, _cost in book_snap.positions}
    expected_positions = dict(book_positions)
    expected_cash = book_snap.cash
    for order in ledger_orders.values():
        if order.broker_order_id is None:
            continue
        known = {f.fill_id for f in ledger.fills(order.client_order_id)}
        for fill in adapter.fills(order.broker_order_id):
            if fill.fill_id in known:
                continue
            if order.side == "BUY":
                expected_cash -= fill.qty * fill.price + fill.fee
                expected_positions[fill.symbol] = (
                    expected_positions.get(fill.symbol, 0) + fill.qty)
            else:
                expected_cash += fill.qty * fill.price - fill.fee
                expected_positions[fill.symbol] = (
                    expected_positions.get(fill.symbol, 0) - fill.qty)

    # --- positions: strict whole-share quantity equality per symbol -------
    for symbol in sorted(set(expected_positions) | set(venue_positions)):
        expected = expected_positions.get(symbol, 0)
        theirs = venue_positions.get(symbol)
        venue_qty = theirs.qty if theirs else 0
        if venue_qty != expected:
            diffs.append(Diff(FATAL, "POSITION_MISMATCH",
                              f"{symbol}: expected={expected} "
                              f"book={book_positions.get(symbol, 0)} "
                              f"venue={venue_qty}"))
        elif venue_qty != book_positions.get(symbol, 0):
            diffs.append(Diff(WARN, "POSITION_SETTLING",
                              f"{symbol}: book={book_positions.get(symbol, 0)} "
                              f"venue={venue_qty} (fills pending)"))

    # --- cash ------------------------------------------------------------
    if abs(expected_cash - venue_snap.cash) > cash_epsilon:
        diffs.append(Diff(FATAL, "CASH_MISMATCH",
                          f"expected={expected_cash} book={book_snap.cash} "
                          f"venue={venue_snap.cash}"))
    elif book_snap.cash != venue_snap.cash:
        diffs.append(Diff(WARN, "CASH_DRIFT",
                          f"book={book_snap.cash} venue={venue_snap.cash}"))

    # --- venue-open orders must all be known and identical ----------------
    for broker_id, venue_order in venue_open.items():
        match = next((o for o in ledger_orders.values()
                      if o.broker_order_id == broker_id), None)
        if match is None:
            diffs.append(Diff(FATAL, "VENUE_ORDER_UNKNOWN",
                              f"{broker_id} {venue_order.symbol} "
                              f"{venue_order.side} {venue_order.qty}"))
        elif (match.symbol != venue_order.symbol or match.side != venue_order.side
                or match.qty != venue_order.qty
                or match.limit_price != venue_order.limit_price):
            diffs.append(Diff(FATAL, "ORDER_FIELD_MISMATCH",
                              f"{broker_id}: ledger={match.symbol}/{match.side}/"
                              f"{match.qty}/{match.limit_price} venue="
                              f"{venue_order.symbol}/{venue_order.side}/"
                              f"{venue_order.qty}/{venue_order.limit_price}"))

    # --- ledger order states vs venue truth -------------------------------
    for order in ledger_open.values():
        venue = (adapter.order_status(order.broker_order_id)
                 if order.broker_order_id
                 else adapter.order_by_client(order.client_order_id))
        if order.status == "CREATED":
            if venue is not None:
                diffs.append(Diff(FATAL, "VENUE_ORDER_UNKNOWN",
                                  f"{order.client_order_id} exists at venue "
                                  "but ledger never sent it"))
            else:
                diffs.append(Diff(WARN, "UNDELIVERED_INTENT",
                                  order.client_order_id))
            continue
        if order.status == "SENT":
            diffs.append(Diff(WARN, "LOST_ACK",
                              f"{order.client_order_id} venue="
                              f"{venue.status if venue else 'absent'}"))
            continue
        if venue is None:
            diffs.append(Diff(FATAL, "LEDGER_ORDER_MISSING",
                              f"{order.client_order_id} {order.status} "
                              "unknown to venue"))
            continue
        if venue.status in ("ACCEPTED", "PARTIALLY_FILLED"):
            if order.status == "CANCEL_PENDING":
                diffs.append(Diff(WARN, "CANCEL_IN_FLIGHT",
                                  order.client_order_id))
        elif venue.status == "CANCELLED" and order.status == "CANCEL_PENDING":
            # Venue finished the cancel; the ledger has not landed it yet.
            diffs.append(Diff(WARN, "CANCEL_SETTLING", order.client_order_id))
        else:
            # Venue reached a terminal state the ledger has not caught up to;
            # settlement lands via the manager's sync().
            diffs.append(Diff(WARN, f"UNSETTLED_{venue.status}",
                              f"{order.client_order_id} ledger={order.status} "
                              f"venue={venue.status}"))

    # --- ledger-terminal orders still working at venue --------------------
    for order in ledger_orders.values():
        if order.status not in _TERMINAL or order.broker_order_id is None:
            continue
        venue = adapter.order_status(order.broker_order_id)
        if venue is not None and venue.status in ("ACCEPTED", "PARTIALLY_FILLED"):
            diffs.append(Diff(FATAL, "STALE_VENUE_ORDER",
                              f"{order.broker_order_id} ledger={order.status} "
                              f"venue={venue.status}"))

    # --- fills: venue-ahead heals via sync; ledger-ahead is impossible -----
    for order in ledger_orders.values():
        if order.broker_order_id is None:
            continue
        venue = adapter.order_status(order.broker_order_id)
        if venue is None:
            continue
        ledger_fills = {f.fill_id for f in ledger.fills(order.client_order_id)}
        venue_fills = {f.fill_id for f in adapter.fills(order.broker_order_id)}
        if venue_fills - ledger_fills:
            diffs.append(Diff(WARN, "UNSETTLED_FILLS",
                              f"{order.client_order_id}: venue ahead by "
                              f"{sorted(venue_fills - ledger_fills)}"))
        if ledger_fills - venue_fills:
            diffs.append(Diff(FATAL, "LEDGER_FILL_UNKNOWN",
                              f"{order.client_order_id}: ledger ahead by "
                              f"{sorted(ledger_fills - venue_fills)}"))

    # --- reserved cash ---------------------------------------------------
    # A book hold is "explained" while it is book-only (CREATED/SENT intents,
    # unlinked reservations) or mid-settlement (cancel pending, venue already
    # terminal, fills awaiting ledger). The dangerous direction is the venue
    # holding reserve that explained holds cannot account for -> FATAL.
    explained = D(0)
    for ident, _sym, _qty, value in book_snap.pending:
        order = ledger_orders.get(ident)
        venue = None
        unsettled = False
        if order is None:
            diffs.append(Diff(WARN, "UNLINKED_RESERVATION", ident))
        elif order.broker_order_id is not None:
            venue = adapter.order_status(order.broker_order_id)
        elif order.status == "SENT":
            venue = adapter.order_by_client(ident)
        if venue is not None and order is not None:
            unsettled = bool(
                {f.fill_id for f in adapter.fills(venue.broker_order_id)}
                - {f.fill_id for f in ledger.fills(ident)})
        if venue is None or venue.status in VENUE_TERMINAL or unsettled:
            # The venue does not currently hold this reservation: it is
            # book-only (CREATED/SENT/unlinked) or mid-settlement release.
            explained += value
    residual = book_snap.reserved_cash - venue_snap.reserved_cash - explained
    if residual < -cash_epsilon:
        diffs.append(Diff(FATAL, "RESERVED_MISMATCH",
                          f"book={book_snap.reserved_cash} "
                          f"venue={venue_snap.reserved_cash} "
                          f"explained={explained}"))
    elif residual > cash_epsilon:
        # Book over-reserving relative to venue is conservative drift, not
        # divergence — but never silently.
        diffs.append(Diff(WARN, "RESERVED_DRIFT",
                          f"book={book_snap.reserved_cash} "
                          f"venue={venue_snap.reserved_cash} "
                          f"explained={explained}"))

    status = (FATAL if any(d.severity == FATAL for d in diffs)
              else WARN if diffs else CLEAN)
    return Reconciliation(status, book_snap.session_id, at, tuple(diffs),
                          book_snap.cash, venue_snap.cash, len(venue_open))
