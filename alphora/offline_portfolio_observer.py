"""Atomically persist a session-pinned offline loss halt in the paper book.

This deliberately uses existing OfflineReservations SQLite transaction and
persisted halt key rather than creating a second, inconsistent risk store.
No polling, connectivity, fabricated fills, broker actions or auth semantics.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D

from .offline_portfolio_mark import (
    HeldPosition, MarkResult, PortfolioMarkInput, SourceQuote,
    mark_reconciled_paper_portfolio,
)
from .reservations import OfflineReservations, PaperBookSnapshot
from .risk import INITIAL_EQUITY


@dataclass(frozen=True)
class ObservedPortfolioMark:
    valid: bool
    reason: str
    mark: MarkResult | None = None
    book: PaperBookSnapshot | None = None


def observe_offline_portfolio_mark(
    book: OfflineReservations,
    quotes: tuple[SourceQuote, ...],
    *,
    as_of: datetime,
    reconciled: bool = False,
    session_verified: bool = False,
    mode: str = "paper",
) -> ObservedPortfolioMark:
    """Read the current book, calculate bid-side equity, and persist a loss stop.

    A single BEGIN IMMEDIATE transaction serializes snapshot extraction,
    valuation and the SQLite halt write. Invalid/unreconciled observations
    return NO book or usable account. An already halted book cannot unhalt.

    Important: caller claims of quote source and exchange session are NOT
    authenticated. This is a manually invoked offline risk observation, not
    a required guard on every subsequent ledger mutation, nor a trade API.
    """
    if mode != "paper":
        return ObservedPortfolioMark(False, "REAL_TRADING_DISABLED")
    if type(book) is not OfflineReservations or reconciled is not True:
        return ObservedPortfolioMark(False, "INVALID_OR_UNRECONCILED_BOOK")
    if session_verified is not True:
        return ObservedPortfolioMark(False, "UNVERIFIED_SESSION")

    # Existing reservations own the halt and all book accounting. Do NOT
    # hold only a read transaction: a synthetic fill could otherwise change
    # holdings after we mark them but before the loss halt commits.
    with book._write():
        snapshot = book._snapshot()
        if snapshot.pending:
            return ObservedPortfolioMark(False, "PENDING_ORDER_RECONCILIATION")
        # snapshot.halted includes the temporary CHECKING/FATAL guard.
        # Value using the durable halt flag instead: a safe quote during
        # CHECKING must not become a terminal loss halt, while a true breach
        # must still persist even if the public snapshot is already halted.
        persistent_halt = book._meta("halted") == "1"
        input_snapshot = PortfolioMarkInput(
            session_id=snapshot.session_id,
            session_open_equity=INITIAL_EQUITY,
            cash=snapshot.cash,
            positions=tuple(HeldPosition(sym, qty)
                            for sym, qty, _basis in snapshot.positions),
            reconciled=True,
            pending_state_clear=True,
            halted=persistent_halt,
        )
        mark = mark_reconciled_paper_portfolio(
            input_snapshot, quotes, as_of=as_of, mode=mode,
            session_verified=session_verified, policy=book._policy,
        )
        if not mark.valid or mark.account is None:
            return ObservedPortfolioMark(False, mark.reason)
        if (mark.account.cash != snapshot.cash
                or mark.account.session_id != snapshot.session_id):
            # Defense in depth against mismatched local accounting inputs.
            return ObservedPortfolioMark(False, "ACCOUNT_RECONCILIATION_FAILED")
        if mark.halt_required and not persistent_halt:
            cursor = book._db.execute(
                "UPDATE book_meta SET value='1' WHERE key='halted' AND value='0'"
            )
            if cursor.rowcount != 1:
                raise ValueError("PERSISTENT_HALT_UPDATE_FAILED")
        confirmed = book._snapshot()
        if mark.halt_required and book._meta("halted") != "1":
            # A CHECKING/FATAL guard cannot prove the terminal loss halt
            # survived; require the durable storage bit inside this txn.
            raise ValueError("PERSISTENT_HALT_VERIFICATION_FAILED")
        return ObservedPortfolioMark(True, mark.reason, mark, confirmed)
