"""Durable *offline* reservation accounting for hypothetical paper order intents.

No brokers, orders, networks, quotes or strategy execution exist. BUY intents
still require an external reconciled account and a fresh risk decision; SELL
intents only consume internally tracked positions (they reduce exposure, so no
risk-gate call is needed) and remain blocked while the book is halted.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal as D
from pathlib import Path
import re
import sqlite3

from .risk import BuyProposal, PaperAccount, INITIAL_EQUITY, DAILY_LOSS_FRACTION, RISK_FRACTION, validate_buy

_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")


def _money(value, *, positive=False):
    return isinstance(value, D) and value.is_finite() and (value > 0 if positive else value >= 0)


def _integer_shares(value):
    return type(value) is int and 0 < value <= 1_000_000_000


def _decimal(value):
    try:
        return D(value)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError("BOOK_STATE_CORRUPT") from exc


@dataclass(frozen=True)
class PaperBookSnapshot:
    cash: D
    reserved_cash: D
    available_cash: D
    positions: tuple[tuple[str, int, D], ...]
    pending: tuple[tuple[str, str, int, D], ...]
    pending_sells: tuple[tuple[str, str, int], ...]
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
                self._db.execute("""CREATE TABLE IF NOT EXISTS sell_intents (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0),
                    limit_price TEXT NOT NULL, fee_budget TEXT NOT NULL,
                    filled INTEGER NOT NULL DEFAULT 0,
                    fees_paid TEXT NOT NULL DEFAULT '0', status TEXT NOT NULL DEFAULT 'OPEN'
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS hypothetical_sell_fills (
                    id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES sell_intents(id),
                    qty INTEGER NOT NULL CHECK(qty>0), price TEXT NOT NULL, fee TEXT NOT NULL
                )""")
                meta = dict(self._db.execute("SELECT key,value FROM book_meta"))
                if not meta:
                    if (self._db.execute("SELECT 1 FROM buy_intents LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM hypothetical_fills LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM sell_intents LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM hypothetical_sell_fills LIMIT 1").fetchone()):
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
            limit, stop, budget, paid = map(_decimal, (limit, stop, budget, paid))
            if (not isinstance(ident, str) or not _ID.fullmatch(ident)
                    or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                    or not _integer_shares(qty) or type(filled) is not int
                    or not 0 <= filled <= qty
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
        bought = {}
        fill_totals = {}
        spent_cash = D(0)
        for fill_id, intent_id, qty, price, fee in self._db.execute(
            "SELECT id,intent_id,qty,price,fee FROM hypothetical_fills"
        ):
            price, fee = _decimal(price), _decimal(fee)
            if (not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                    or intent_id not in intents or not _integer_shares(qty)
                    or not _money(price, positive=True) or not _money(fee)
                    or not intents[intent_id][3] < price <= intents[intent_id][2]):
                raise ValueError("BOOK_STATE_CORRUPT")
            old_qty, old_fees = fill_totals.get(intent_id, (0, D(0)))
            fill_totals[intent_id] = (old_qty + qty, old_fees + fee)
            symbol = intents[intent_id][0]
            old_qty, old_cost = bought.get(symbol, (0, D(0)))
            bought[symbol] = old_qty + qty, old_cost + qty * price + fee
            spent_cash += qty * price + fee
        sell_intents = {}
        pending_sells = []
        for ident, symbol, qty, limit, budget, filled, paid, status in self._db.execute(
            "SELECT id,symbol,qty,limit_price,fee_budget,filled,fees_paid,status "
            "FROM sell_intents ORDER BY id"
        ):
            limit, budget, paid = map(_decimal, (limit, budget, paid))
            if (not isinstance(ident, str) or not _ID.fullmatch(ident)
                    or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                    or not _integer_shares(qty) or type(filled) is not int
                    or not 0 <= filled <= qty
                    or not _money(limit, positive=True) or not _money(budget)
                    or not _money(paid) or paid > budget
                    or status not in ("OPEN", "FILLED", "CANCELLED")
                    or (status == "FILLED") != (filled == qty)
                    or (status == "OPEN" and filled == qty)):
                raise ValueError("BOOK_STATE_CORRUPT")
            sell_intents[ident] = (symbol, limit, filled, paid)
            if status == "OPEN":
                pending_sells.append((ident, symbol, qty - filled))
        sell_totals = {}
        sold = {}
        earned_cash = D(0)
        for fill_id, intent_id, qty, price, fee in self._db.execute(
            "SELECT id,intent_id,qty,price,fee FROM hypothetical_sell_fills"
        ):
            price, fee = _decimal(price), _decimal(fee)
            intent = sell_intents.get(intent_id)
            if (not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                    or intent is None or not _integer_shares(qty)
                    or not _money(price, positive=True) or not _money(fee)
                    or price < intent[1]):
                raise ValueError("BOOK_STATE_CORRUPT")
            old_qty, old_fees = sell_totals.get(intent_id, (0, D(0)))
            sell_totals[intent_id] = (old_qty + qty, old_fees + fee)
            symbol = intent[0]
            old_qty, old_proceeds = sold.get(symbol, (0, D(0)))
            sold[symbol] = old_qty + qty, old_proceeds + qty * price - fee
            earned_cash += qty * price - fee
        positions = {}
        for symbol, (bought_qty, cost) in bought.items():
            sold_qty = sold.get(symbol, (0, D(0)))[0]
            remaining = bought_qty - sold_qty
            if remaining < 0:
                raise ValueError("BOOK_STATE_CORRUPT")
            if remaining:
                positions[symbol] = (remaining, cost * remaining / bought_qty)
        pending_sell_qty = {}
        for _ident, symbol, remaining_qty in pending_sells:
            pending_sell_qty[symbol] = pending_sell_qty.get(symbol, 0) + remaining_qty
        if (any(symbol not in bought for symbol in sold)
                or any(pending > positions.get(symbol, (0, D(0)))[0]
                       for symbol, pending in pending_sell_qty.items())):
            raise ValueError("BOOK_STATE_CORRUPT")
        if (reserved > cash or cash + spent_cash - earned_cash != INITIAL_EQUITY
                or any(fill_totals.get(ident, (0, D(0))) != (row[5], row[6])
                       for ident, row in intents.items())
                or any(sell_totals.get(ident, (0, D(0))) != (row[2], row[3])
                       for ident, row in sell_intents.items())):
            raise ValueError("BOOK_STATE_CORRUPT")
        return PaperBookSnapshot(
            cash, reserved, cash - reserved,
            tuple((sym, *positions[sym]) for sym in sorted(positions)),
            tuple(active), tuple(pending_sells), halted == "1",
            self._meta("session_id"))

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

    def observe_daily_loss(self, account: PaperAccount, *, reconciled: bool = False):
        """Persist a one-way offline halt from a caller-asserted account mark.

        This is an explicit observation operation, NOT a live monitoring feed.
        Reconciliation and mark authenticity remain the caller's responsibility.
        It must also run on idempotent reservation retries, which otherwise skip
        the pre-trade risk gate.
        """
        if not isinstance(account, PaperAccount) or reconciled is not True:
            raise ValueError("INVALID_OR_UNRECONCILED_ACCOUNT")
        with self._write():
            snapshot = self._snapshot()
            if snapshot.halted:
                return snapshot
            if (account.session_id != snapshot.session_id
                    or account.session_open_equity != INITIAL_EQUITY
                    or account.cash != snapshot.cash):
                raise ValueError("UNRECONCILED_CASH_OR_SESSION")
            if (type(account.equity) is not D or not _money(account.equity, positive=True)
                    or type(account.day_pnl) is not D or not account.day_pnl.is_finite()
                    or type(account.halted) is not bool):
                raise ValueError("INVALID_ACCOUNT_STATE")
            day_change = account.equity - INITIAL_EQUITY
            loss_floor = -(INITIAL_EQUITY * DAILY_LOSS_FRACTION)
            if (account.halted or account.day_pnl <= loss_floor
                    or day_change <= loss_floor):
                self._db.execute("UPDATE book_meta SET value='1' WHERE key='halted'")
                return self._snapshot()
            if (account.cash > account.equity
                    or abs(day_change - account.day_pnl) > D("0.01")):
                raise ValueError("INCONSISTENT_ACCOUNT_STATE")
            return snapshot

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
        # Preserve the established already-halted rejection contract while
        # recording new loss observations even on idempotent retries.
        if self.snapshot().halted:
            raise ValueError("PAPER_BOOK_HALTED")
        observed = self.observe_daily_loss(account, reconciled=reconciled)
        if observed.halted:
            raise ValueError("RISK_VETO:DAILY_LOSS_HALT")
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
                "SELECT symbol,qty,limit_price,stop_price,fee_budget,status FROM buy_intents WHERE id=?",
                (intent_id,)).fetchone()
            fields = (proposal.symbol, proposal.quantity, proposal.price,
                      proposal.stop_price, fee_budget)
            if existing is not None:
                old = (existing[0], D(existing[1]), D(existing[2]),
                       D(existing[3]), D(existing[4]))
                if old != fields:
                    raise ValueError("CONFLICTING_INTENT_ID")
                # A cancelled intent must never appear to have been reserved
                # again: cancellation permanently releases its pending cash.
                # Reuse a NEW id to request any subsequent virtual exposure.
                if existing[5] == "CANCELLED":
                    raise ValueError("CANCELLED_INTENT_ID")
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
                # Reserve the *new* intent's fee budget against the
                # instrument cap too. Earlier pending fees and filled fees
                # are already included in exposure, but this intent's budget
                # would otherwise be omitted from the 10% limit.
                replace(proposal, existing_instrument_value=exposure + fee_budget),
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
                # A maximum synthetic fee is potential loss at the stop.
                # The pre-trade guard checks price-to-stop risk alone, so
                # including this budget prevents a fee-only risk-cap bypass.
                if ((proposal.price - proposal.stop_price) * proposal.quantity
                        + fee_budget > account.equity * RISK_FRACTION):
                    raise ValueError("RISK_VETO:TRADE_RISK_EXCEEDED")
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

    def reserve_sell(self, intent_id: str, symbol: str, qty: int,
                     limit_price: D, *, fee_budget: D = D(0)):
        """Reserve held shares for a hypothetical SELL intent; never routed.

        Sells only consume internally tracked positions, so no external
        account assertion or risk-gate call applies — but the book-wide halt
        still blocks new intents and new fills.
        """
        if (not isinstance(intent_id, str) or not _ID.fullmatch(intent_id)
                or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or not _integer_shares(qty)
                or not _money(limit_price, positive=True) or not _money(fee_budget)):
            raise ValueError("INVALID_SELL_INTENT")
        if self.snapshot().halted:
            raise ValueError("PAPER_BOOK_HALTED")
        with self._write():
            snapshot = self._snapshot()
            if snapshot.halted:
                raise ValueError("PAPER_BOOK_HALTED")
            existing = self._db.execute(
                "SELECT symbol,qty,limit_price,fee_budget,status "
                "FROM sell_intents WHERE id=?", (intent_id,)).fetchone()
            if existing is not None:
                fields = (existing[0], existing[1], _decimal(existing[2]),
                          _decimal(existing[3]))
                if fields != (symbol, qty, limit_price, fee_budget):
                    raise ValueError("CONFLICTING_INTENT_ID")
                if existing[4] == "CANCELLED":
                    raise ValueError("CANCELLED_INTENT_ID")
                return self._snapshot()
            held = next((q for sym, q, _cost in snapshot.positions
                         if sym == symbol), 0)
            pending = sum((remaining for _i, sym, remaining
                           in snapshot.pending_sells if sym == symbol), 0)
            if qty > held - pending:
                raise ValueError("INSUFFICIENT_POSITION")
            self._db.execute(
                "INSERT INTO sell_intents(id,symbol,qty,limit_price,fee_budget) "
                "VALUES(?,?,?,?,?)",
                (intent_id, symbol, qty, str(limit_price), str(fee_budget)))
            return self._snapshot()

    def record_sell_fill(self, intent_id: str, fill_id: str,
                         qty: int, price: D, *, fee: D = D(0)):
        """Record an *invented offline* SELL fill, crediting net proceeds.

        Sell fills must respect the stored minimum limit price, stay within
        quantity and fee budgets, and never exceed recorded holdings.
        """
        if (not isinstance(intent_id, str) or not _ID.fullmatch(intent_id)
                or not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                or not _integer_shares(qty) or not _money(price, positive=True)
                or not _money(fee)):
            raise ValueError("INVALID_SYNTHETIC_FILL")
        with self._write():
            existing = self._db.execute(
                "SELECT intent_id,qty,price,fee FROM hypothetical_sell_fills "
                "WHERE id=?", (fill_id,)).fetchone()
            if existing:
                if existing != (intent_id, qty, str(price), str(fee)):
                    if (existing[0] != intent_id or existing[1] != qty or
                            D(existing[2]) != price or D(existing[3]) != fee):
                        raise ValueError("CONFLICTING_FILL_ID")
                return self._snapshot()
            if self._meta("halted") == "1":
                raise ValueError("PAPER_BOOK_HALTED")
            row = self._db.execute(
                "SELECT symbol,qty,filled,limit_price,fee_budget,fees_paid,status "
                "FROM sell_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None or row[6] != "OPEN":
                raise ValueError("NO_OPEN_INTENT")
            symbol, total, filled, limit, budget, paid, _open = row
            if (filled + qty > total or price < D(limit)
                    or fee + D(paid) > D(budget)):
                raise ValueError("FILL_EXCEEDS_LIMITS")
            snapshot = self._snapshot()
            held = next((q for sym, q, _cost in snapshot.positions
                         if sym == symbol), 0)
            if qty > held:
                raise ValueError("FILL_EXCEEDS_POSITION")
            self._db.execute(
                "INSERT INTO hypothetical_sell_fills(id,intent_id,qty,price,fee) "
                "VALUES(?,?,?,?,?)",
                (fill_id, intent_id, qty, str(price), str(fee)))
            self._db.execute(
                "UPDATE book_meta SET value=? WHERE key='cash'",
                (str(D(self._meta("cash")) + qty * price - fee),))
            self._db.execute(
                "UPDATE sell_intents SET filled=?, fees_paid=?, status=? WHERE id=?",
                (filled + qty, str(D(paid) + fee),
                 "FILLED" if filled + qty == total else "OPEN", intent_id))
            return self._snapshot()

    def cancel_sell(self, intent_id: str):
        """Release the un-filled share reservation of a sell intent."""
        if not isinstance(intent_id, str) or not _ID.fullmatch(intent_id):
            raise ValueError("INVALID_INTENT_ID")
        with self._write():
            row = self._db.execute(
                "SELECT status FROM sell_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise ValueError("UNKNOWN_INTENT")
            if row[0] == "OPEN":
                self._db.execute(
                    "UPDATE sell_intents SET status='CANCELLED' WHERE id=?",
                    (intent_id,))
            return self._snapshot()
