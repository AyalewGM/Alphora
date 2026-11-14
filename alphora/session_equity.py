"""Inert, transactional paper session-equity checkpoint contract (P0 #42).

Not wired to PaperSessionRunner or the OMS. Callers must first attest
reconciled book/ledger/venue state, verified and complete marks, calendar
identity and pending-order state. Synthetic receipts are not market provenance.
No public API, broker access, or unattended multi-session execution.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D, DecimalException, Inexact, localcontext
from itertools import count
import re
import sqlite3

from .risk import DAILY_LOSS_FRACTION, INITIAL_EQUITY

_RECEIPT = re.compile(r"[a-f0-9]{64}\Z")
_CENT = D("0.01")
_SAVEPOINT_SEQUENCE = count()


def _session(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError("INVALID_EXCHANGE_SESSION")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("INVALID_EXCHANGE_SESSION") from exc
    if parsed.isoformat() != value:
        raise ValueError("INVALID_EXCHANGE_SESSION")
    return parsed


def _amount(value, *, positive=False):
    if type(value) is not D or not value.is_finite():
        raise ValueError("INVALID_SESSION_EQUITY")
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            ctx.traps[Inexact] = True
            if value < 0 or (positive and value <= 0) or value != value.quantize(_CENT):
                raise ValueError("INVALID_SESSION_EQUITY")
    except DecimalException as exc:
        raise ValueError("INVALID_SESSION_EQUITY") from exc
    return value


def _receipt(value):
    if type(value) is not str or not _RECEIPT.fullmatch(value):
        raise ValueError("INVALID_SESSION_RECEIPT")
    return value


@dataclass(frozen=True)
class SessionEquityState:
    session_id: str
    opening_equity: D
    last_equity: D
    opening_receipt: str
    last_mark_receipt: str | None
    revision: int
    observations: int
    halted: bool


class SessionEquityCheckpoint:
    """One durable state row in the SAME SQLite connection as the paper book.

    Operations use BEGIN IMMEDIATE, or join the caller's current transaction;
    the owning OMS must eventually atomically bind its own session revision.
    An isolated checkpoint alone cannot authorize rollover or BUY dispatch.
    """

    def __init__(self, db: sqlite3.Connection):
        if not isinstance(db, sqlite3.Connection):
            raise ValueError("INVALID_SESSION_DATABASE")
        self._db = db

    @contextmanager
    def _atomic(self):
        # A checkpoint may share the owning book's transaction. Roll back
        # *this operation's* partial writes even if its caller catches an
        # exception and later commits unrelated book work.
        own = not self._db.in_transaction
        savepoint = None if own else f"alphora_session_checkpoint_{next(_SAVEPOINT_SEQUENCE)}"
        if own:
            self._db.execute("BEGIN IMMEDIATE")
        else:
            # Generated internally, never user-controlled SQL.
            self._db.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            if own:
                self._db.execute("COMMIT")
            else:
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            if own:
                self._db.execute("ROLLBACK")
            else:
                self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def bootstrap(self, session_id, opening_equity, opening_receipt, *, proven_new_book=False):
        """Only the book constructor may provide a proven fresh-book gate.

        Never call this on an old SQLite file, even if currently flat. The
        untrusted boolean is intentionally NOT a substitute for book evidence.
        """
        _session(session_id)
        _amount(opening_equity, positive=True)
        _receipt(opening_receipt)
        if opening_equity != INITIAL_EQUITY:
            raise ValueError("INVALID_SESSION_STARTING_EQUITY")
        if proven_new_book is not True:
            raise ValueError("FRESH_BOOK_EVIDENCE_REQUIRED")
        with self._atomic():
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='session_equity_checkpoint'").fetchone():
                raise ValueError("SESSION_CHECKPOINT_EXISTS")
            self._db.execute("""CREATE TABLE session_equity_checkpoint (
                id INTEGER PRIMARY KEY CHECK(id=1),
                session_id TEXT NOT NULL, opening_equity TEXT NOT NULL,
                last_equity TEXT NOT NULL, opening_receipt TEXT NOT NULL,
                last_mark_receipt TEXT, revision INTEGER NOT NULL,
                observations INTEGER NOT NULL, halted INTEGER NOT NULL
            )""")
            self._db.execute("INSERT INTO session_equity_checkpoint VALUES"
                             "(1,?,?,?,?,NULL,0,0,0)",
                             (session_id, str(opening_equity), str(opening_equity),
                              opening_receipt))
            return self._read()

    def _read(self):
        try:
            rows = self._db.execute("SELECT * FROM session_equity_checkpoint").fetchall()
            if len(rows) != 1:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            (key, sid, opening, last, opening_hash, mark_hash,
             revision, observations, halted) = rows[0]
            _session(sid)
            # SQLite accepts arbitrary text despite TEXT affinity. Bound stored
            # Decimal representations before parsing/multiplication: an oversized
            # or malformed persisted amount is corrupt, never a clean risk state.
            if (type(opening) is not str or type(last) is not str
                    or len(opening) > 64 or len(last) > 64):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            opening = _amount(D(opening), positive=True)
            last = _amount(D(last))
            _receipt(opening_hash)
            if mark_hash is not None:
                _receipt(mark_hash)
            if (key != 1 or type(revision) is not int or revision < 0
                    or type(observations) is not int or observations < 0
                    or halted not in (0, 1) or type(halted) is not int
                    or (observations == 0) != (mark_hash is None)
                    or revision < observations):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            # No observation may change the opening mark. Once a verified mark
            # reaches the inclusive 2% loss floor, the halt must be durable;
            # a stored unhalted row below that floor is never trusted.
            with localcontext() as ctx:
                ctx.prec = 80
                ctx.traps[Inexact] = True
                floor = opening * (D(1) - DAILY_LOSS_FRACTION)
            if ((observations == 0 and last != opening)
                    or (observations > 0 and last <= floor and halted == 0)):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            return SessionEquityState(sid, opening, last, opening_hash,
                                      mark_hash, revision, observations, bool(halted))
        except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("SESSION_CHECKPOINT_CORRUPT") from exc

    def snapshot(self):
        with self._atomic():
            return self._read()

    def observe(self, *, session_id, equity, receipt, expected_revision,
                reconciled=False, marks_verified=False):
        """One-way 2% halt; exact boundary counts, no recovery on price rebound.

        Verified flags must come from a trusted future integration, never UI.
        Missing/stale marks are a veto, not a fabricated zero-loss observation.
        """
        _session(session_id)
        _amount(equity)
        _receipt(receipt)
        if reconciled is not True or marks_verified is not True:
            raise ValueError("UNVERIFIED_SESSION_MARK")
        with self._atomic():
            old = self._read()
            if old.session_id != session_id:
                raise ValueError("SESSION_MISMATCH")
            if old.last_mark_receipt == receipt:
                if old.last_equity != equity:
                    raise ValueError("CONFLICTING_SESSION_MARK_REPLAY")
                return old
            if type(expected_revision) is not int or expected_revision != old.revision:
                raise ValueError("STALE_SESSION_REVISION")
            with localcontext() as ctx:
                ctx.prec = 80
                ctx.traps[Inexact] = True
                floor = old.opening_equity * (D(1) - DAILY_LOSS_FRACTION)
                halted = old.halted or equity <= floor
            self._db.execute("UPDATE session_equity_checkpoint SET "
                             "last_equity=?,last_mark_receipt=?,revision=?,"
                             "observations=?,halted=? WHERE id=1",
                             (str(equity), receipt, old.revision + 1,
                              old.observations + 1, int(halted)))
            return self._read()

    def rollover(self, *, previous_session, next_session, opening_equity,
                 opening_receipt, expected_revision, reconciled=False,
                 zero_pending=False, verified_calendar=False):
        """Durable idempotent transition, NOT integrated OMS rollover.

        The next opening equity must exactly match the previous final verified
        mark; external cash flows/corporate actions remain unsupported.
        """
        _session(previous_session)
        _session(next_session)
        _amount(opening_equity, positive=True)
        _receipt(opening_receipt)
        with self._atomic():
            old = self._read()
            if old.session_id == next_session:
                if (old.opening_equity == opening_equity
                        and old.opening_receipt == opening_receipt):
                    return old
                raise ValueError("CONFLICTING_SESSION_REPLAY")
            if (old.session_id != previous_session
                    or _session(next_session) <= _session(previous_session)):
                raise ValueError("SESSION_MISMATCH")
            if old.halted:
                raise ValueError("PERSISTED_SAFETY_HALT")
            if (reconciled is not True or zero_pending is not True
                    or verified_calendar is not True or old.observations == 0):
                raise ValueError("ROLLOVER_NOT_VERIFIED")
            if type(expected_revision) is not int or expected_revision != old.revision:
                raise ValueError("STALE_SESSION_REVISION")
            if opening_equity != old.last_equity:
                raise ValueError("OPENING_EQUITY_MISMATCH")
            self._db.execute("UPDATE session_equity_checkpoint SET "
                             "session_id=?,opening_equity=?,last_equity=?,"
                             "opening_receipt=?,last_mark_receipt=NULL,"
                             "revision=?,observations=0 WHERE id=1",
                             (next_session, str(opening_equity), str(opening_equity),
                              opening_receipt, old.revision + 1))
            return self._read()

    def halt(self):
        """Manual/FATAL permanent veto; no clear method exists."""
        with self._atomic():
            old = self._read()
            if not old.halted:
                self._db.execute("UPDATE session_equity_checkpoint "
                                 "SET halted=1,revision=? WHERE id=1",
                                 (old.revision + 1,))
            return self._read()
