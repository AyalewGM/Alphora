"""Durable *offline* reservation accounting for hypothetical paper BUY intents.

No brokers, orders, networks, quotes, strategy execution or sell accounting exist.
External account/position reconciliation and a fresh risk decision remain mandatory.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal as D
from pathlib import Path
import re
import sqlite3

from .risk import BuyProposal, PaperAccount, INITIAL_EQUITY, validate_buy

_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")


def _money(value, *, positive=False):
    return isinstance(value, D) and value.is_finite() and (value > 0 if positive else value >= 0)


def _integer_shares(value):
    return type(value) is int and 0 < value <= 1_000_000_000


def _decimal(value):
    return D(value)


@dataclass(frozen=True)
class PaperBookSnapshot:
    cash: D
    reserved_cash: D
    available_cash: D
    positions: tuple[tuple[str, int, D], ...]
    pending: tuple[tuple[str, str, int, D], ...]
    halted: bool
    session_id: str


class OfflineReservations:
    """SQLite-transactional cash reservations and synthetic partial-fill receipts.

    This is **not** an order management system or executable broker adapter.
    It does not derive equity from current market quotes, nor support exits,
    corporate actions, costs beyond caller-budgeted fees or session rollover.
    """

    def __init__(self, path, *, session_id: str, starting_cash: D = INITIAL_EQUITY):
        if (not isinstance(path, (str, Path)) or not str(path).strip()
                or str(path).lower().startswith("file:")
                or str(path) == ":memory:" or "\x00" in str(path)
                or not isinstance(session_id, str) or not _ID.fullmatch(session_id)
                or type(starting_cash) is not D or starting_cash != INITIAL_EQUITY):
            raise ValueError("INVALID_PAPER_BOOK")
        self._db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._write():
                self._db.execute("CREATE TABLE IF NOT EXISTS book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self._db.execute("""CREATE TABLE IF NOT EXISTS buy_intents (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0),
                    limit_price TEXT NOT NULL, stop_price TEXT NOT NULL,
                    fee_budget TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
                    fees_paid TEXT NOT NULL DEFAULT '0', status TEXT NOT NULL DEFAULT 'OPEN'
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS hypothetical_fills (
                    id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES buy_intents(id),
                    qty INTEGER NOT NULL CHECK(qty>0), price TEXT NOT NULL, fee TEXT NOT NULL
                )""")
                meta = dict(self._db.execute("SELECT key,value FROM book_meta"))
                if not meta:
                    if (self._db.execute("SELECT 1 FROM buy_intents LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM hypothetical_fills LIMIT 1").fetchone()):
                        raise ValueError("BOOK_STATE_CORRUPT")
                    self._db.executemany("INSERT INTO book_meta(key,value) VALUES(?,?)", [
                        ("session_id", session_id), ("starting_cash", str(starting_cash)),
                        ("cash", str(starting_cash)), ("halted", "0"),
                    ])
                elif (meta.get("session_id") != session_id or
                      _decimal(meta.get("starting_cash", "0")) != starting_cash or
                      set(meta) != {"session_id", "starting_cash", "cash", "halted"}):
                    raise ValueError("BOOK_REOPEN_MISMATCH")
                # Reopening a corrupted persisted book must fail before any
                # new reservations or hypothetical fills can be recorded.
                self._snapshot()
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _write(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def close(self):
        self._db.close()

    def _meta(self, key):
        item = self._db.execute("SELECT value FROM book_meta WHERE key=?", (key,)).fetchone()
        if item is None:
            raise ValueError("BOOK_STATE_CORRUPT")
        return item[0]

    def _snapshot(self):
        cash = _decimal(self._meta("cash"))
        halted = self._meta("halted")
        if not _money(cash) or halted not in ("0", "1"):
            raise ValueError("BOOK_STATE_CORRUPT")
        active = []
        reserved = D(0)
        intents = {}
        for ident, symbol, qty, limit, stop, budget, filled, paid, status in self._db.execute(
            "SELECT id,symbol,qty,limit_price,stop_price,fee_budget,filled,fees_paid,status "
            "FROM buy_intents ORDER BY id"
        ):
            limit, stop, budget, paid = map(D, (limit, stop, budget, paid))
            if (not _integer_shares(qty) or not 0 <= filled <= qty
                    or not _money(limit, positive=True) or not _money(stop, positive=True)
                    or stop >= limit or not _money(budget)
                    or not _money(paid) or paid > budget
                    or status not in ("OPEN", "FILLED", "CANCELLED")
                    or (status == "FILLED") != (filled == qty)
                    or (status == "OPEN" and filled == qty)):
                raise ValueError("BOOK_STATE_CORRUPT")
            intents[ident] = (symbol, qty, limit, stop, budget, filled, paid)
            if status == "OPEN":
                remaining = (qty - filled) * limit + budget - paid
                reserved += remaining
                active.append((ident, symbol, qty - filled, remaining))
        holdings = {}
        fill_totals = {}
        spent_cash = D(0)
        for intent_id, qty, price, fee in self._db.execute(
            "SELECT intent_id,qty,price,fee FROM hypothetical_fills"
        ):
            price, fee = D(price), D(fee)
            if (intent_id not in intents or not _integer_shares(qty)
                    or not _money(price, positive=True) or not _money(fee)
                    or not intents[intent_id][3] < price <= intents[intent_id][2]):
                raise ValueError("BOOK_STATE_CORRUPT")
            old_qty, old_fees = fill_totals.get(intent_id, (0, D(0)))
            fill_totals[intent_id] = (old_qty + qty, old_fees + fee)
            symbol = intents[intent_id][0]
            old_qty, old_cost = holdings.get(symbol, (0, D(0)))
            holdings[symbol] = old_qty + qty, old_cost + qty * price
            spent_cash += qty * price + fee
        if (reserved > cash or cash + spent_cash != INITIAL_EQUITY
                or any(fill_totals.get(ident, (0, D(0))) != (row[5], row[6])
                       for ident, row in intents.items())):
            raise ValueError("BOOK_STATE_CORRUPT")
        return PaperBookSnapshot(cash, reserved, cash - reserved,
                                 tuple((sym, *holdings[sym]) for sym in sorted(holdings)),
                                 tuple(active), halted == "1", self._meta("session_id"))

    def snapshot(self):
        self._db.execute("BEGIN")
        try:
            result = self._snapshot()
            self._db.execute("COMMIT")
            return result
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def halt(self):
        """One-way manual pause for this session (including across restarts)."""
        with self._write():
            self._db.execute("UPDATE book_meta SET value='1' WHERE key='halted'")

    def reserve_buy(self, intent_id: str, account: PaperAccount,
                    proposal: BuyProposal, *, fee_budget: D = D(0),
                    reconciled: bool = False):
        """Reserve virtual cash; NEVER communicate this intent to any venue."""
        if (not isinstance(intent_id, str) or not _ID.fullmatch(intent_id)
                or not isinstance(account, PaperAccount)
                or not isinstance(proposal, BuyProposal)
                or not _money(fee_budget)
                or reconciled is not True):
            raise ValueError("INVALID_OR_UNRECONCILED_INTENT")
        with self._write():
            snapshot = self._snapshot()
            if snapshot.halted:
                raise ValueError("PAPER_BOOK_HALTED")
            if (account.session_id != snapshot.session_id or
                    account.session_open_equity != INITIAL_EQUITY or
                    account.cash != snapshot.cash or account.halted):
                raise ValueError("UNRECONCILED_CASH_OR_SESSION")
            if not isinstance(proposal.symbol, str) or not _SYMBOL.fullmatch(proposal.symbol):
                raise ValueError("INVALID_SYMBOL")
            existing = self._db.execute(
                "SELECT symbol,qty,limit_price,stop_price,fee_budget FROM buy_intents WHERE id=?",
                (intent_id,)).fetchone()
            fields = (proposal.symbol, proposal.quantity, proposal.price,
                      proposal.stop_price, fee_budget)
            if existing is not None:
                old = (existing[0], D(existing[1]), D(existing[2]),
                       D(existing[3]), D(existing[4]))
                if old != fields:
                    raise ValueError("CONFLICTING_INTENT_ID")
                return self._snapshot()  # stable dedupe, no second reservation
            pending_symbol = sum(
                (row[3] for row in snapshot.pending if row[1] == proposal.symbol), D(0)
            )
            filled_cost = next((cost for sym, _, cost in snapshot.positions
                                if sym == proposal.symbol), D(0))
            if not _money(proposal.existing_instrument_value):
                raise ValueError("INVALID_POSITION_VALUE")
            exposure = max(proposal.existing_instrument_value, filled_cost) + pending_symbol
            guard = validate_buy(
                replace(account, cash=snapshot.available_cash),
                replace(proposal, existing_instrument_value=exposure),
                expected_session_id=snapshot.session_id, mode="paper",
            )
            if not guard.permitted:
                if guard.reason != "DAILY_LOSS_HALT":
                    raise ValueError(f"RISK_VETO:{guard.reason}")
                # Crossing the 2% daily-loss limit is a one-way shutdown,
                # not merely a rejection of this particular proposal.
                # Commit the halt in the same transaction as the risk check;
                # raising inside _write() would roll back the shutdown.
                self._db.execute("UPDATE book_meta SET value='1' WHERE key='halted'")
            else:
                if proposal.quantity != proposal.quantity.to_integral_value():
                    raise ValueError("FRACTIONAL_SHARES")
                quantity = int(proposal.quantity)
                if not _integer_shares(quantity):
                    raise ValueError("INVALID_QUANTITY")
                reserve = proposal.quantity * proposal.price + fee_budget
                if reserve > snapshot.available_cash:
                    raise ValueError("INSUFFICIENT_UNRESERVED_CASH")
                self._db.execute(
                    "INSERT INTO buy_intents(id,symbol,qty,limit_price,stop_price,fee_budget) VALUES(?,?,?,?,?,?)",
                    (intent_id, proposal.symbol, quantity, str(proposal.price),
                     str(proposal.stop_price), str(fee_budget)),
                )
                return self._snapshot()
        # The only successful transaction without a reservation is the
        # durable risk shutdown; keep the public veto contract unchanged.
        raise ValueError("RISK_VETO:DAILY_LOSS_HALT")

    def record_hypothetical_fill(self, intent_id: str, fill_id: str,
                                 qty: int, price: D, *, fee: D = D(0)):
        """Record an *invented offline simulation fill*, not a broker event."""
        if (not isinstance(intent_id, str) or not _ID.fullmatch(intent_id)
                or not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                or not _integer_shares(qty) or not _money(price, positive=True)
                or not _money(fee)):
            raise ValueError("INVALID_SYNTHETIC_FILL")
        with self._write():
            existing = self._db.execute(
                "SELECT intent_id,qty,price,fee FROM hypothetical_fills WHERE id=?",
                (fill_id,),
            ).fetchone()
            if existing:
                if existing != (intent_id, qty, str(price), str(fee)):
                    # Values with numerically equal but different decimal spelling
                    # count as the same semantic fill receipt.
                    if (existing[0] != intent_id or existing[1] != qty or
                            D(existing[2]) != price or D(existing[3]) != fee):
                        raise ValueError("CONFLICTING_FILL_ID")
                return self._snapshot()
            if self._meta("halted") == "1":
                raise ValueError("PAPER_BOOK_HALTED")
            row = self._db.execute(
                "SELECT qty,filled,limit_price,stop_price,fee_budget,fees_paid,status "
                "FROM buy_intents WHERE id=?", (intent_id,),
            ).fetchone()
            if row is None or row[6] != "OPEN":
                raise ValueError("NO_OPEN_INTENT")
            total, filled, limit, stop, fee_budget, fees_paid, _ = row
            if (filled + qty > total or price > D(limit) or
                    price <= D(stop) or fee + D(fees_paid) > D(fee_budget)):
                raise ValueError("FILL_EXCEEDS_LIMITS")
            cost = qty * price + fee
            if cost > D(self._meta("cash")):
                raise ValueError("INSUFFICIENT_CASH_AT_FILL")
            self._db.execute(
                "INSERT INTO hypothetical_fills(id,intent_id,qty,price,fee) VALUES(?,?,?,?,?)",
                (fill_id, intent_id, qty, str(price), str(fee)),
            )
            self._db.execute(
                "UPDATE book_meta SET value=? WHERE key='cash'",
                (str(D(self._meta("cash")) - cost),),
            )
            self._db.execute(
                "UPDATE buy_intents SET filled=?, fees_paid=?, status=? WHERE id=?",
                (filled + qty, str(D(fees_paid) + fee),
                 "FILLED" if filled + qty == total else "OPEN", intent_id),
            )
            return self._snapshot()

    def cancel_remaining(self, intent_id: str):
        if not isinstance(intent_id, str) or not _ID.fullmatch(intent_id):
            raise ValueError("INVALID_INTENT_ID")
        with self._write():
            row = self._db.execute("SELECT status FROM buy_intents WHERE id=?",
                                   (intent_id,)).fetchone()
            if row is None:
                raise ValueError("UNKNOWN_INTENT")
            if row[0] == "OPEN":
                self._db.execute("UPDATE buy_intents SET status='CANCELLED' WHERE id=?", (intent_id,))
            return self._snapshot()
