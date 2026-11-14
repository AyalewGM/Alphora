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
import json
import re
import sqlite3

from .paper_config import PaperRiskConfig
from .risk import DAILY_LOSS_FRACTION

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
    policy: PaperRiskConfig
    policy_receipt: str


def _policy_record(value):
    """Bound and canonically fingerprint an inert policy before persistence.

    Proposal values are NOT proof of an applied or authorized policy. The
    eventual book owner must supply authenticated effective-policy evidence.
    """
    if type(value) is not PaperRiskConfig:
        raise ValueError("INVALID_SESSION_POLICY")
    scalars = (value.starting_equity_usd, value.risk_per_trade_fraction,
               value.max_instrument_fraction, value.daily_loss_fraction)
    for number in scalars:
        if type(number) is not D or not number.is_finite():
            raise ValueError("INVALID_SESSION_POLICY")
        parts = number.as_tuple()
        if len(parts.digits) > 64 or not -64 <= parts.exponent <= 64:
            raise ValueError("INVALID_SESSION_POLICY")
    try:
        wire = value.to_wire()
        if any(len(wire[name]) > 64 for name in
               ("starting_equity_usd", "risk_per_trade_fraction",
                "max_instrument_fraction", "daily_loss_fraction")):
            raise ValueError("INVALID_SESSION_POLICY")
        checked = PaperRiskConfig.from_wire(wire)
        if checked != value or checked.daily_loss_fraction > DAILY_LOSS_FRACTION:
            raise ValueError("INVALID_SESSION_POLICY")
        literal = json.dumps(wire, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True)
        if len(literal) > 384:
            raise ValueError("INVALID_SESSION_POLICY")
        return literal, checked.receipt(), checked
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("INVALID_SESSION_POLICY") from exc


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

    def bootstrap(self, session_id, opening_equity, opening_receipt, *,
                  proven_new_book=False, policy=None):
        """Only the book constructor may provide a proven fresh-book gate.

        Never call this on an old SQLite file, even if currently flat. The
        untrusted boolean is intentionally NOT a substitute for book evidence.
        """
        _session(session_id)
        _amount(opening_equity, positive=True)
        _receipt(opening_receipt)
        # The approved $100k/2% settings remain defaults. A *new* paper
        # portfolio may request tighter limits or different starting capital;
        # neither setting can silently reconfigure an existing SQLite book.
        policy_text, policy_hash, checked = _policy_record(
            PaperRiskConfig() if policy is None else policy)
        if opening_equity != checked.starting_equity_usd:
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
                observations INTEGER NOT NULL, halted INTEGER NOT NULL,
                policy_json TEXT NOT NULL, policy_receipt TEXT NOT NULL
            )""")
            # The new-portfolio policy is immutable. Any later authorized
            # effective-policy change requires its own audited OMS migration.
            self._db.execute("""CREATE TRIGGER session_equity_policy_no_update
                BEFORE UPDATE OF policy_json,policy_receipt
                ON session_equity_checkpoint
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_POLICY'); END""")
            # Append-only session mark receipts share the book's transaction.
            # A previously used receipt must not overwrite any newer mark.
            self._db.execute("""CREATE TABLE session_equity_marks (
                session_id TEXT NOT NULL, receipt TEXT NOT NULL,
                equity TEXT NOT NULL, observation_number INTEGER NOT NULL
                    CHECK(observation_number >= 1),
                PRIMARY KEY (session_id, receipt),
                UNIQUE (session_id, observation_number)
            )""")
            self._db.execute("""CREATE TRIGGER session_equity_marks_no_update
                BEFORE UPDATE ON session_equity_marks
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_MARK'); END""")
            self._db.execute("""CREATE TRIGGER session_equity_marks_no_delete
                BEFORE DELETE ON session_equity_marks
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_MARK'); END""")

            # Anchor the original session and every rollover separately from
            # the mutable active checkpoint. A later revision may NEVER
            # change the opening equity or session ID without a linked
            # final-mark transition. These local witnesses do not authenticate
            # operator authority or external market/calendar provenance.
            self._db.execute("""CREATE TABLE session_equity_genesis (
                id INTEGER PRIMARY KEY CHECK(id=1),
                session_id TEXT NOT NULL, opening_equity TEXT NOT NULL,
                opening_receipt TEXT NOT NULL
            )""")
            self._db.execute("""CREATE TRIGGER session_equity_genesis_no_update
                BEFORE UPDATE ON session_equity_genesis
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_GENESIS'); END""")
            self._db.execute("""CREATE TRIGGER session_equity_genesis_no_delete
                BEFORE DELETE ON session_equity_genesis
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_GENESIS'); END""")
            self._db.execute("""CREATE TABLE session_equity_rollovers (
                ordinal INTEGER PRIMARY KEY CHECK(ordinal >= 1),
                previous_session TEXT NOT NULL, next_session TEXT NOT NULL UNIQUE,
                previous_mark_receipt TEXT NOT NULL, opening_receipt TEXT NOT NULL,
                opening_equity TEXT NOT NULL, revision INTEGER NOT NULL
            )""")
            self._db.execute("""CREATE TRIGGER session_equity_rollovers_no_update
                BEFORE UPDATE ON session_equity_rollovers
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_ROLLOVER'); END""")
            self._db.execute("""CREATE TRIGGER session_equity_rollovers_no_delete
                BEFORE DELETE ON session_equity_rollovers
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_ROLLOVER'); END""")
            self._db.execute("INSERT INTO session_equity_genesis VALUES(1,?,?,?)",
                             (session_id, str(opening_equity), opening_receipt))
            # One-time, append-only safety halt evidence. A recovered mark
            # cannot erase an earlier loss breach; non-price/manual halts
            # require their own durable witness.
            self._db.execute("""CREATE TABLE session_equity_halt_events (
                session_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL CHECK(reason IN ('LOSS','MANUAL')),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                mark_receipt TEXT
            )""")
            self._db.execute("""CREATE TRIGGER session_equity_halts_no_update
                BEFORE UPDATE ON session_equity_halt_events
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_HALT'); END""")
            self._db.execute("""CREATE TRIGGER session_equity_halts_no_delete
                BEFORE DELETE ON session_equity_halt_events
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_HALT'); END""")
            self._db.execute("INSERT INTO session_equity_checkpoint VALUES"
                             "(1,?,?,?,?,NULL,0,0,0,?,?)",
                             (session_id, str(opening_equity), str(opening_equity),
                              opening_receipt, policy_text, policy_hash))
            return self._read()

    def _read(self):
        try:
            rows = self._db.execute("SELECT * FROM session_equity_checkpoint").fetchall()
            if len(rows) != 1:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            (key, sid, opening, last, opening_hash, mark_hash,
             revision, observations, halted, policy_text, policy_hash) = rows[0]
            _session(sid)
            # SQLite accepts arbitrary text despite TEXT affinity. Bound stored
            # Decimal representations before parsing/multiplication: an oversized
            # or malformed persisted amount is corrupt, never a clean risk state.
            if (type(opening) is not str or type(last) is not str
                    or len(opening) > 64 or len(last) > 64):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            opening = _amount(D(opening), positive=True)
            last = _amount(D(last))
            if type(policy_text) is not str or len(policy_text) > 384:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            policy_literal, expected_hash, effective = _policy_record(
                PaperRiskConfig.from_wire(json.loads(policy_text)))
            _receipt(policy_hash)
            if policy_literal != policy_text or expected_hash != policy_hash:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            _receipt(opening_hash)
            if mark_hash is not None:
                _receipt(mark_hash)
            if (key != 1 or type(revision) is not int or revision < 0
                    or type(observations) is not int or observations < 0
                    or halted not in (0, 1) or type(halted) is not int
                    or (observations == 0) != (mark_hash is None)
                    or revision < observations
                    or (revision == 0 and opening != effective.starting_equity_usd)):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            # No observation may change the opening mark. Once a verified mark
            # reaches the inclusive 2% loss floor, the halt must be durable;
            # a stored unhalted row below that floor is never trusted.
            with localcontext() as ctx:
                ctx.prec = 80
                ctx.traps[Inexact] = True
                floor = opening * (D(1) - effective.daily_loss_fraction)
            if ((observations == 0 and last != opening)
                    or (observations > 0 and last <= floor and halted == 0)):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            # Recheck the full durable per-session receipt sequence. Missing,
            # contradictory, out-of-order, or truncated rows fail closed.
            history = self._db.execute(
                "SELECT receipt,equity,observation_number FROM session_equity_marks "
                "WHERE session_id=? ORDER BY observation_number", (sid,)).fetchall()
            if len(history) != observations:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            prior_breach = False
            breached_receipts = set()
            for index, (past_receipt, past_equity, ordinal) in enumerate(history, start=1):
                _receipt(past_receipt)
                if (type(ordinal) is not int or ordinal != index
                        or type(past_equity) is not str or len(past_equity) > 64):
                    raise ValueError("SESSION_CHECKPOINT_CORRUPT")
                parsed_equity = _amount(D(past_equity))
                if parsed_equity <= floor:
                    prior_breach = True
                    breached_receipts.add(past_receipt)
                if index == observations and (past_receipt != mark_hash
                                              or parsed_equity != last):
                    raise ValueError("SESSION_CHECKPOINT_CORRUPT")

            # Validate the whole session-open chain, including previous
            # sessions' final marks. Checking only the current receipt history
            # would permit a stale/forged opening baseline after rollover.
            genesis = self._db.execute(
                "SELECT id,session_id,opening_equity,opening_receipt "
                "FROM session_equity_genesis").fetchall()
            if len(genesis) != 1:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            g_key, chain_session, chain_opening_text, chain_receipt = genesis[0]
            if (g_key != 1 or type(chain_opening_text) is not str
                    or len(chain_opening_text) > 64):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            _session(chain_session)
            _receipt(chain_receipt)
            chain_opening = _amount(D(chain_opening_text), positive=True)
            if chain_opening != effective.starting_equity_usd:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            transitions = self._db.execute(
                "SELECT ordinal,previous_session,next_session,"
                "previous_mark_receipt,opening_receipt,opening_equity,revision "
                "FROM session_equity_rollovers ORDER BY ordinal").fetchall()
            prior_revision = -1
            for index, (ordinal, previous, following, final_receipt,
                        next_receipt, next_equity_text, transition_revision) in enumerate(
                            transitions, start=1):
                if (type(ordinal) is not int or ordinal != index
                        or previous != chain_session
                        or _session(following) <= _session(previous)
                        or type(transition_revision) is not int
                        or not prior_revision < transition_revision <= revision
                        or type(next_equity_text) is not str
                        or len(next_equity_text) > 64):
                    raise ValueError("SESSION_CHECKPOINT_CORRUPT")
                _receipt(final_receipt)
                _receipt(next_receipt)
                next_equity = _amount(D(next_equity_text), positive=True)
                prior_marks = self._db.execute(
                    "SELECT receipt,equity,observation_number "
                    "FROM session_equity_marks WHERE session_id=? "
                    "ORDER BY observation_number", (previous,)).fetchall()
                if not prior_marks:
                    raise ValueError("SESSION_CHECKPOINT_CORRUPT")
                with localcontext() as ctx:
                    ctx.prec = 80
                    ctx.traps[Inexact] = True
                    previous_floor = chain_opening * (
                        D(1) - effective.daily_loss_fraction)
                for mark_index, (prior_receipt, prior_equity_text, number) in enumerate(
                        prior_marks, start=1):
                    if (type(number) is not int or number != mark_index
                            or type(prior_equity_text) is not str
                            or len(prior_equity_text) > 64):
                        raise ValueError("SESSION_CHECKPOINT_CORRUPT")
                    _receipt(prior_receipt)
                    prior_equity = _amount(D(prior_equity_text))
                    # No previously breached session may have rolled over.
                    if prior_equity <= previous_floor:
                        raise ValueError("SESSION_CHECKPOINT_CORRUPT")
                if (prior_marks[-1][0] != final_receipt
                        or prior_equity != next_equity):
                    raise ValueError("SESSION_CHECKPOINT_CORRUPT")
                chain_session, chain_opening, chain_receipt = (
                    following, next_equity, next_receipt)
                prior_revision = transition_revision
            if (sid != chain_session or opening != chain_opening
                    or opening_hash != chain_receipt):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            # A historic breach stays binding even after a later price
            # recovery. A manual/FATAL halt must have a non-price witness.
            halt_rows = self._db.execute(
                "SELECT session_id,reason,revision,mark_receipt "
                "FROM session_equity_halt_events").fetchall()
            # Rollover of a halted session is forbidden. A witness from an
            # older session cannot be silently bypassed by changing the
            # checkpoint's active session ID.
            if (len(halt_rows) > 1 or bool(halt_rows) != bool(halted)
                    or (halt_rows and halt_rows[0][0] != sid)):
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            if prior_breach and not halted:
                raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            if halt_rows:
                _, reason, event_revision, event_receipt = halt_rows[0]
                if (type(event_revision) is not int or event_revision < 1
                        or event_revision > revision
                        or (reason == "LOSS" and
                            (event_receipt not in breached_receipts or
                             type(event_receipt) is not str))
                        or (reason == "MANUAL" and event_receipt is not None)
                        or reason not in ("LOSS", "MANUAL")):
                    raise ValueError("SESSION_CHECKPOINT_CORRUPT")
            return SessionEquityState(sid, opening, last, opening_hash,
                                      mark_hash, revision, observations, bool(halted),
                                      effective, policy_hash)
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
            # Non-adjacent replay is idempotent, not a new observation.
            # Compare every previous receipt in this exchange session.
            prior = self._db.execute(
                "SELECT equity FROM session_equity_marks WHERE session_id=? AND receipt=?",
                (session_id, receipt)).fetchone()
            if prior is not None:
                if _amount(D(prior[0])) != equity:
                    raise ValueError("CONFLICTING_SESSION_MARK_REPLAY")
                return old
            if type(expected_revision) is not int or expected_revision != old.revision:
                raise ValueError("STALE_SESSION_REVISION")
            with localcontext() as ctx:
                ctx.prec = 80
                ctx.traps[Inexact] = True
                floor = old.opening_equity * (D(1) - old.policy.daily_loss_fraction)
                halted = old.halted or equity <= floor
            self._db.execute(
                "INSERT INTO session_equity_marks "
                "(session_id,receipt,equity,observation_number) VALUES (?,?,?,?)",
                (session_id, receipt, str(equity), old.observations + 1))
            if halted and not old.halted:
                self._db.execute(
                    "INSERT INTO session_equity_halt_events "
                    "(session_id,reason,revision,mark_receipt) VALUES (?,?,?,?)",
                    (session_id, "LOSS", old.revision + 1, receipt))
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
                # A retry is valid only for the exact, previously committed
                # transition. Matching the active opening alone mistakenly
                # treats genesis (or a forged predecessor) as a rollover.
                prior = self._db.execute(
                    "SELECT previous_session FROM session_equity_rollovers "
                    "WHERE next_session=?", (next_session,)).fetchone()
                if (prior is not None and prior[0] == previous_session
                        and old.opening_equity == opening_equity
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
            self._db.execute(
                "INSERT INTO session_equity_rollovers "
                "(ordinal,previous_session,next_session,previous_mark_receipt,"
                "opening_receipt,opening_equity,revision) "
                "VALUES ((SELECT COALESCE(MAX(ordinal),0)+1 FROM "
                "session_equity_rollovers),?,?,?,?,?,?)",
                (previous_session, next_session, old.last_mark_receipt,
                 opening_receipt, str(opening_equity), old.revision + 1))
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
                self._db.execute(
                    "INSERT INTO session_equity_halt_events "
                    "(session_id,reason,revision,mark_receipt) VALUES (?,?,?,NULL)",
                    (old.session_id, "MANUAL", old.revision + 1))
                self._db.execute("UPDATE session_equity_checkpoint "
                                 "SET halted=1,revision=? WHERE id=1",
                                 (old.revision + 1,))
            return self._read()
