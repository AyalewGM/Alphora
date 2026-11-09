"""Durable *offline* BUY reservations and bounded hypothetical SELL receipts.

No brokers, network, quotes, automated strategies or authenticated fills exist.
External reconciliation and independently verified market marks remain mandatory.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal as D, DecimalException, Inexact, ROUND_HALF_EVEN, localcontext
from pathlib import Path
import re
import sqlite3

from .risk import (BuyProposal, PaperAccount, RiskPolicy, DEFAULT_RISK_POLICY,
                   INITIAL_EQUITY, DAILY_LOSS_FRACTION, validate_buy)

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
    halted: bool
    session_id: str
    realized_pnl: D = D("0")


class OfflineReservations:
    """SQLite-transactional cash reservations and synthetic partial-fill receipts.

    This is **not** an order management system or executable broker adapter.
    It does not derive equity from current market quotes, nor support exits,
    corporate actions, costs beyond caller-budgeted fees or session rollover.
    """

    def __init__(self, path, *, session_id: str, starting_cash: D = INITIAL_EQUITY,
                 policy: RiskPolicy = DEFAULT_RISK_POLICY):
        if (not isinstance(path, (str, Path)) or not str(path).strip()
                or str(path).lower().startswith("file:")
                or str(path) == ":memory:" or "\x00" in str(path)
                or not isinstance(session_id, str) or not _ID.fullmatch(session_id)
                or type(starting_cash) is not D or starting_cash != INITIAL_EQUITY
                or not isinstance(policy, RiskPolicy)):
            raise ValueError("INVALID_PAPER_BOOK")
        self._policy = policy
        # Policy identity is pinned to a session; it cannot be loosened during
        # a running/reopened book without creating a new reviewed session.
        if not policy.fingerprint():
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
                self._db.execute("""CREATE TABLE IF NOT EXISTS hypothetical_sells (
                    id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
                    symbol TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0),
                    price TEXT NOT NULL, fee TEXT NOT NULL,
                    basis_retired TEXT NOT NULL
                )""")
                meta = dict(self._db.execute("SELECT key,value FROM book_meta"))
                if not meta:
                    if (self._db.execute("SELECT 1 FROM buy_intents LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM hypothetical_fills LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM hypothetical_sells LIMIT 1").fetchone()):
                        raise ValueError("BOOK_STATE_CORRUPT")
                    self._db.executemany("INSERT INTO book_meta(key,value) VALUES(?,?)", [
                        ("session_id", session_id), ("starting_cash", str(starting_cash)),
                        ("cash", str(starting_cash)), ("halted", "0"),
                        ("risk_policy", policy.fingerprint()),
                    ])
                elif (meta.get("session_id") != session_id or
                      _decimal(meta.get("starting_cash", "0")) != starting_cash or
                      (set(meta) != {"session_id", "starting_cash", "cash", "halted", "risk_policy"}
                       or meta.get("risk_policy") != policy.fingerprint())):
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
                try:
                    with localcontext() as context:
                        context.traps[Inexact] = True
                        remaining = (qty - filled) * limit + budget - paid
                        reserved += remaining
                except DecimalException as exc:
                    raise ValueError("BOOK_STATE_CORRUPT") from exc
                active.append((ident, symbol, qty - filled, remaining))
        holdings = {}
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
            symbol = intents[intent_id][0]
            held_qty, old_cost = holdings.get(symbol, (0, D(0)))
            try:
                with localcontext() as context:
                    context.traps[Inexact] = True
                    updated_fees = old_fees + fee
                    updated_cost = old_cost + qty * price + fee
                    spent_cash += qty * price + fee
            except DecimalException as exc:
                raise ValueError("BOOK_STATE_CORRUPT") from exc
            fill_totals[intent_id] = (old_qty + qty, updated_fees)
            holdings[symbol] = held_qty + qty, updated_cost
        # Replay SELLs in sequence after all BUYs. New BUYs are forbidden
        # after the first SELL in this bounded phase, so chronological order
        # cannot be reordered to create synthetic short exposure.
        sold_cash = D(0)
        realized = D(0)
        sell_sequence = 0
        rows = self._db.execute(
            "SELECT id,sequence,symbol,qty,price,fee,basis_retired "
            "FROM hypothetical_sells ORDER BY sequence").fetchall()
        if rows and active:
            raise ValueError("BOOK_STATE_CORRUPT")
        for sell_id, sequence, symbol, qty, price_raw, fee_raw, basis_raw in rows:
            price, fee, basis_retired = map(_decimal, (price_raw, fee_raw, basis_raw))
            old_qty, old_cost = holdings.get(symbol, (0, D(0)))
            if (not isinstance(sell_id, str) or not _ID.fullmatch(sell_id)
                    or type(sequence) is not int or sequence != sell_sequence + 1
                    or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                    or not _integer_shares(qty) or qty > old_qty
                    or not _money(price, positive=True) or not _money(fee)
                    or not _money(basis_retired, positive=True)
                    or fee > price * qty or old_qty < 1):
                raise ValueError("BOOK_STATE_CORRUPT")
            try:
                with localcontext() as context:
                    context.prec = 50
                    # Partial cost basis is deliberately half-even cent
                    # rounded; unlike cash arithmetic it may be inexact.
                    context.traps[Inexact] = False
                    expected_basis = (old_cost if qty == old_qty else
                                      (old_cost * qty / old_qty).quantize(
                                          D("0.01"), rounding=ROUND_HALF_EVEN))
                    proceeds = qty * price - fee
                    sold_cash += proceeds
                    realized += proceeds - expected_basis
                    remaining_cost = old_cost - expected_basis
            except DecimalException as exc:
                raise ValueError("BOOK_STATE_CORRUPT") from exc
            if (expected_basis != basis_retired or remaining_cost < 0
                    or (qty != old_qty and remaining_cost <= 0)):
                raise ValueError("BOOK_STATE_CORRUPT")
            if qty == old_qty:
                del holdings[symbol]
            else:
                holdings[symbol] = (old_qty - qty, remaining_cost)
            sell_sequence += 1
        try:
            with localcontext() as context:
                context.traps[Inexact] = True
                reconstructed_cash = cash + spent_cash - sold_cash
                available_cash = cash - reserved
        except DecimalException as exc:
            raise ValueError("BOOK_STATE_CORRUPT") from exc
        if (reserved > cash or reconstructed_cash != INITIAL_EQUITY
                or any(fill_totals.get(ident, (0, D(0))) != (row[5], row[6])
                       for ident, row in intents.items())):
            raise ValueError("BOOK_STATE_CORRUPT")
        return PaperBookSnapshot(cash, reserved, available_cash,
                                 tuple((sym, *holdings[sym]) for sym in sorted(holdings)),
                                 tuple(active), halted == "1", self._meta("session_id"),
                                 realized)

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
            loss_floor = -(INITIAL_EQUITY * DAILY_LOSS_FRACTION)
            # Compare absolute equity directly with the exact $98k limit
            # before any subtraction could round a genuine daily loss away.
            if (account.halted or account.day_pnl <= loss_floor
                    or account.equity <= INITIAL_EQUITY + loss_floor):
                self._db.execute("UPDATE book_meta SET value='1' WHERE key='halted'")
                return self._snapshot()
            # A finite Decimal can still overflow the active arithmetic
            # context, or round a near-boundary P&L comparison. Such marks
            # are not a trustworthy reconciled snapshot: veto fail-closed.
            try:
                with localcontext() as context:
                    context.traps[Inexact] = True
                    day_change = account.equity - INITIAL_EQUITY
                    pnl_gap = abs(day_change - account.day_pnl)
            except DecimalException as exc:
                raise ValueError("UNREPRESENTABLE_ACCOUNT_MARK") from exc
            if account.cash > account.equity or pnl_gap > D("0.01"):
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
            if self._db.execute("SELECT 1 FROM hypothetical_sells LIMIT 1").fetchone():
                raise ValueError("BUY_AFTER_SELL_UNSUPPORTED")
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
            if not _money(proposal.existing_instrument_value):
                raise ValueError("INVALID_POSITION_VALUE")
            # Never round away a tiny positive fee when existing pending
            # exposure is near the 10% instrument cap.
            try:
                with localcontext() as context:
                    context.traps[Inexact] = True
                    pending_symbol = sum(
                        (row[3] for row in snapshot.pending if row[1] == proposal.symbol), D(0)
                    )
                    filled_cost = next((cost for sym, _, cost in snapshot.positions
                                        if sym == proposal.symbol), D(0))
                    exposure = max(proposal.existing_instrument_value, filled_cost) + pending_symbol
                    exposure_with_fees = exposure + fee_budget
            except DecimalException as exc:
                raise ValueError("RISK_VETO:UNREPRESENTABLE_LEDGER_ARITHMETIC") from exc
            guard = validate_buy(
                replace(account, cash=snapshot.available_cash),
                # Reserve the *new* intent's fee budget against the
                # instrument cap too. Earlier pending fees and filled fees
                # are already included in exposure, but this intent's budget
                # would otherwise be omitted from the 10% limit.
                replace(proposal, existing_instrument_value=exposure_with_fees),
                expected_session_id=snapshot.session_id, mode="paper",
                policy=self._policy,
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
                try:
                    with localcontext() as context:
                        context.traps[Inexact] = True
                        loss_with_fees = ((proposal.price - proposal.stop_price) *
                                          proposal.quantity + fee_budget)
                        maximum_loss = account.equity * self._policy.risk_per_trade_fraction
                        reserve = proposal.quantity * proposal.price + fee_budget
                except DecimalException as exc:
                    raise ValueError("RISK_VETO:UNREPRESENTABLE_LEDGER_ARITHMETIC") from exc
                if loss_with_fees > maximum_loss:
                    raise ValueError("RISK_VETO:TRADE_RISK_EXCEEDED")
                if proposal.quantity != proposal.quantity.to_integral_value():
                    raise ValueError("FRACTIONAL_SHARES")
                quantity = int(proposal.quantity)
                if not _integer_shares(quantity):
                    raise ValueError("INVALID_QUANTITY")
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
            if self._db.execute("SELECT 1 FROM hypothetical_sells LIMIT 1").fetchone():
                raise ValueError("BUY_AFTER_SELL_UNSUPPORTED")
            row = self._db.execute(
                "SELECT qty,filled,limit_price,stop_price,fee_budget,fees_paid,status "
                "FROM buy_intents WHERE id=?", (intent_id,),
            ).fetchone()
            if row is None or row[6] != "OPEN":
                raise ValueError("NO_OPEN_INTENT")
            total, filled, limit, stop, fee_budget, fees_paid, _ = row
            # Every synthetic receipt must preserve its full cash and fee
            # liability. A subcent fee may otherwise round out of cost,
            # reporting more virtual cash than is truly available.
            try:
                with localcontext() as context:
                    context.traps[Inexact] = True
                    updated_fees = D(fees_paid) + fee
                    cost = qty * price + fee
                    cash_before = D(self._meta("cash"))
                    cash_after = cash_before - cost
            except DecimalException as exc:
                raise ValueError("UNREPRESENTABLE_LEDGER_ARITHMETIC") from exc
            if (filled + qty > total or price > D(limit) or
                    price <= D(stop) or updated_fees > D(fee_budget)):
                raise ValueError("FILL_EXCEEDS_LIMITS")
            if cost > cash_before:
                raise ValueError("INSUFFICIENT_CASH_AT_FILL")
            self._db.execute(
                "INSERT INTO hypothetical_fills(id,intent_id,qty,price,fee) VALUES(?,?,?,?,?)",
                (fill_id, intent_id, qty, str(price), str(fee)),
            )
            self._db.execute(
                "UPDATE book_meta SET value=? WHERE key='cash'",
                (str(cash_after),),
            )
            self._db.execute(
                "UPDATE buy_intents SET filled=?, fees_paid=?, status=? WHERE id=?",
                (filled + qty, str(updated_fees),
                 "FILLED" if filled + qty == total else "OPEN", intent_id),
            )
            return self._snapshot()


    def record_hypothetical_sell(self, fill_id: str, symbol: str,
                                 qty: int, price: D, *, fee: D = D(0),
                                 allow_halted: bool = False):
        """Atomically record a caller-invented SELL receipt in the BUY book.

        This is deliberately NOT a sell order/authorization API. It has no
        quote, timestamp, external fill provenance or automated executor.
        SELLs require a fully resolved BUY book; after the first SELL, no
        additional BUY may be reserved in this limited accounting phase.
        With allow_halted=True, a trusted, independently reconciled reduce-only
        paper manager may settle an existing long position during a persistent
        DAILY_LOSS_HALT. Manual kill files are checked by that manager and
        must never be bypassed by this bookkeeping option. Default denies it.

        """
        if (not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or not _integer_shares(qty)
                or type(price) is not D or not _money(price, positive=True)
                or type(fee) is not D or not _money(fee)
                or type(allow_halted) is not bool):
            raise ValueError("INVALID_SYNTHETIC_SELL")
        with self._write():
            state = self._snapshot()
            existing = self._db.execute(
                "SELECT symbol,qty,price,fee FROM hypothetical_sells WHERE id=?",
                (fill_id,)).fetchone()
            if existing:
                if (existing[0] != symbol or existing[1] != qty
                        or _decimal(existing[2]) != price
                        or _decimal(existing[3]) != fee):
                    raise ValueError("CONFLICTING_SELL_FILL_ID")
                return state
            if state.halted and not allow_halted:
                raise ValueError("PAPER_BOOK_HALTED")
            if state.pending:
                raise ValueError("PENDING_BUY_RECONCILIATION")
            found = next(((held, basis) for name, held, basis in state.positions
                          if name == symbol), None)
            if found is None or qty > found[0]:
                raise ValueError("SYNTHETIC_OVERSELL")
            held, basis = found
            try:
                with localcontext() as context:
                    context.prec = 50
                    gross = price * qty
                    if fee > gross:
                        raise ValueError("SELL_FEE_EXCEEDS_PROCEEDS")
                    retired = (basis if qty == held else
                               (basis * qty / held).quantize(
                                   D("0.01"), rounding=ROUND_HALF_EVEN))
                    proceeds = gross - fee
                    new_cash = state.cash + proceeds
                    # No fractional-cent cash or corrupted basis.
                    if (not new_cash.is_finite() or new_cash < 0
                            or retired <= 0
                            or new_cash != new_cash.quantize(D("0.01"))):
                        raise ValueError("INVALID_SELL_ACCOUNTING")
                next_sequence = self._db.execute(
                    "SELECT COUNT(*) FROM hypothetical_sells").fetchone()[0] + 1
            except DecimalException as exc:
                raise ValueError("INVALID_SELL_ACCOUNTING") from exc
            self._db.execute(
                "INSERT INTO hypothetical_sells"
                "(id,sequence,symbol,qty,price,fee,basis_retired) "
                "VALUES(?,?,?,?,?,?,?)",
                (fill_id, next_sequence, symbol, qty, str(price),
                 str(fee), str(retired)))
            self._db.execute(
                "UPDATE book_meta SET value=? WHERE key='cash'",
                (str(new_cash),))
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
