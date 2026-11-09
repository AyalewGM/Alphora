"""Durable, offline order-lifecycle ledger for PAPER order intents.

This module records the lifecycle of hypothetical paper orders only. It is not
an OMS connected to a venue, a broker adapter, a market data feed, a cash
accountant, or a risk approval. No method contacts a network or any brokerage;
"SENT"/"ACKED"/"FILLED" are durable records of caller or adapter assertions,
not verified venue facts. Cash reservation and release remain the separate
OfflineReservations book's responsibility; atomic coupling of cash and order
state is a later manager milestone. See docs/BROKER_ADAPTER.md.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal as D
from hashlib import sha256
from pathlib import Path
import json
import re
import sqlite3
from time import monotonic, sleep

_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ORDER_ID = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_SESSION = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SIDES = frozenset({"BUY", "SELL"})
_ORDER_TYPES = frozenset({"LIMIT"})
_TIF = frozenset({"DAY"})
_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})
_OPEN = frozenset({"CREATED", "SENT", "ACKED", "PARTIALLY_FILLED", "CANCEL_PENDING"})
_STATUSES = _TERMINAL | _OPEN
_TRANSITIONS = {
    "CREATED": frozenset({"SENT", "CANCELLED", "REJECTED"}),
    "SENT": frozenset({"ACKED", "REJECTED", "CANCELLED"}),
    "ACKED": frozenset({"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING",
                        "REJECTED", "EXPIRED"}),
    "PARTIALLY_FILLED": frozenset({"PARTIALLY_FILLED", "FILLED",
                                   "CANCEL_PENDING", "EXPIRED"}),
    "CANCEL_PENDING": frozenset({"CANCELLED", "FILLED", "EXPIRED"}),
    "FILLED": frozenset(), "CANCELLED": frozenset(),
    "REJECTED": frozenset(), "EXPIRED": frozenset(),
}
_FILLABLE = frozenset({"ACKED", "PARTIALLY_FILLED", "CANCEL_PENDING"})
_EVENT_KINDS = frozenset({"CREATED", "SENT", "ACKED", "FILL",
                          "CANCEL_REQUESTED", "CANCELLED", "REJECTED", "EXPIRED"})
_GENESIS = "0" * 64
_MAX_DETAIL_BYTES = 8192


def _configure_wal(db):
    """Require WAL with bounded retry when concurrent openers hold SQLite locks.

    SQLite's busy_timeout does not guarantee that a simultaneous journal-mode
    PRAGMA will wait. Retry *only* SQLite BUSY/LOCKED, never disk or integrity
    errors. The five-second retry window fails closed on persistent locks;
    each individual SQLite statement may also wait for its busy timeout.
    """
    deadline = monotonic() + 5.0
    while True:
        try:
            row = db.execute("PRAGMA journal_mode=WAL").fetchone()
            if (row is None or len(row) != 1
                    or not isinstance(row[0], str) or row[0].lower() != "wal"):
                raise LedgerIntegrityError("WAL_MODE_REQUIRED")
            return
        except sqlite3.OperationalError as exc:
            error_code = getattr(exc, "sqlite_errorcode", None)
            if (type(error_code) is not int
                    or (error_code & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            sleep(min(0.02, remaining))


class LedgerIntegrityError(RuntimeError):
    """Persisted order ledger no longer satisfies its structural invariants."""


@dataclass(frozen=True)
class OrderView:
    client_order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    limit_price: D
    stop_price: D | None
    time_in_force: str
    status: str
    filled: int
    broker_order_id: str | None
    terminal_reason: str | None
    session_id: str
    mode: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FillView:
    fill_id: str
    client_order_id: str
    qty: int
    price: D
    fee: D
    occurred_at: datetime


def _money(value, *, positive=True):
    return isinstance(value, D) and value.is_finite() and (value > 0 if positive else value >= 0)


def _shares(value):
    return type(value) is int and 0 < value <= 1_000_000_000


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(text):
    if not isinstance(text, str):
        raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerIntegrityError("LEDGER_STATE_CORRUPT") from exc
    if not _aware(parsed):
        raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
    return parsed


def _decimal(text):
    try:
        return D(text)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise LedgerIntegrityError("LEDGER_STATE_CORRUPT") from exc


def _session_date(value):
    if not isinstance(value, str) or not _SESSION.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _detail(value):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("INVALID_EVENT_DETAIL")
    for key, item in value.items():
        if (not isinstance(key, str) or not _ID.fullmatch(key)
                or (item is not None and type(item) not in (str, int, bool))):
            raise ValueError("INVALID_EVENT_DETAIL")
    return value


def _canonical_event(client_order_id, kind, detail, occurred_at):
    normalized = {
        "client_order_id": client_order_id, "kind": kind,
        "detail": _detail(detail), "occurred_at": _iso(occurred_at),
    }
    try:
        data = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("INVALID_EVENT_DETAIL") from exc
    if len(data.encode("utf-8")) > _MAX_DETAIL_BYTES:
        raise ValueError("EVENT_DETAIL_TOO_LARGE")
    return data, sha256(data.encode("utf-8")).hexdigest()


def _digest(previous, canonical):
    return sha256((previous + "\n" + canonical).encode("utf-8")).hexdigest()


class OrderLedger:
    """SQLite-transactional paper order ledger; no execution methods exist.

    Each public mutation re-runs structural and hash-chain verification of the
    persisted ledger before writing, in the spirit of OfflineReservations: a
    corrupted store must fail closed before new records are accepted. This is
    casual-tamper detection, not a cryptographic trust anchor.
    """

    def __init__(self, path, *, session_id: str):
        if (not isinstance(path, (str, Path)) or not str(path).strip()
                or str(path).lower().startswith("file:")
                or str(path) == ":memory:" or "\x00" in str(path)):
            raise ValueError("DURABLE_PATH_REQUIRED")
        if not _session_date(session_id):
            raise ValueError("INVALID_SESSION_ID")
        self._session = session_id
        self._db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            _configure_wal(self._db)
            self._db.execute("PRAGMA synchronous=FULL")
            with self._write():
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS ledger_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self._db.execute("""CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL, side TEXT NOT NULL,
                    qty INTEGER NOT NULL CHECK(qty>0),
                    order_type TEXT NOT NULL, limit_price TEXT NOT NULL,
                    stop_price TEXT, time_in_force TEXT NOT NULL,
                    status TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0,
                    broker_order_id TEXT, terminal_reason TEXT,
                    session_id TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'paper',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS order_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
                    canonical TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS order_fills (
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
                    qty INTEGER NOT NULL CHECK(qty>0),
                    price TEXT NOT NULL, fee TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )""")
                meta = dict(self._db.execute("SELECT key,value FROM ledger_meta"))
                if not meta:
                    if (self._db.execute("SELECT 1 FROM orders LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM order_events LIMIT 1").fetchone()
                            or self._db.execute("SELECT 1 FROM order_fills LIMIT 1").fetchone()):
                        raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
                    self._db.execute(
                        "INSERT INTO ledger_meta(key,value) VALUES('session_id',?)",
                        (session_id,))
                elif set(meta) != {"session_id"} or meta["session_id"] != session_id:
                    raise ValueError("LEDGER_REOPEN_MISMATCH")
                self._verify()
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

    @property
    def session_id(self):
        return self._session

    # ---- persistence verification -------------------------------------

    def _verify(self):
        """Fail closed on any structurally invalid persisted state."""
        meta = dict(self._db.execute("SELECT key,value FROM ledger_meta"))
        if set(meta) != {"session_id"} or not _session_date(meta["session_id"]):
            raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
        orders = {}
        for row in self._db.execute(
            "SELECT client_order_id,symbol,side,qty,order_type,limit_price,"
            "stop_price,time_in_force,status,filled,broker_order_id,"
            "terminal_reason,session_id,mode,created_at,updated_at FROM orders"
        ):
            view = self._row_view(row)
            coherent = {
                "CREATED": view.filled == 0 and view.broker_order_id is None,
                "SENT": view.filled == 0 and view.broker_order_id is None,
                "ACKED": view.filled == 0 and view.broker_order_id is not None,
                "PARTIALLY_FILLED": (0 < view.filled < view.qty
                                     and view.broker_order_id is not None),
                "CANCEL_PENDING": (view.filled < view.qty
                                   and view.broker_order_id is not None),
                "FILLED": (view.filled == view.qty
                           and view.broker_order_id is not None),
                "CANCELLED": view.filled < view.qty,
                "REJECTED": (view.filled == 0
                             and isinstance(view.terminal_reason, str)
                             and bool(view.terminal_reason)),
                "EXPIRED": (view.filled < view.qty
                            and view.broker_order_id is not None),
            }[view.status]
            if (not coherent or view.session_id != meta["session_id"]
                    or view.created_at > view.updated_at
                    or (view.terminal_reason is not None
                        and view.status != "REJECTED")):
                raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
            orders[view.client_order_id] = view
        fill_totals = {}
        for row in self._db.execute(
            "SELECT fill_id,client_order_id,qty,price,fee,occurred_at FROM order_fills"
        ):
            fill = self._fill_view(row)
            order = orders.get(fill.client_order_id)
            if (order is None or order.session_id != meta["session_id"]
                    or (order.side == "BUY" and fill.price > order.limit_price)
                    or (order.side == "SELL" and fill.price < order.limit_price)):
                raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
            key = fill.client_order_id
            fill_totals[key] = fill_totals.get(key, 0) + fill.qty
        for order in orders.values():
            if fill_totals.get(order.client_order_id, 0) != order.filled:
                raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
        rows = self._db.execute(
            "SELECT sequence,event_id,client_order_id,canonical,previous_hash,"
            "record_hash FROM order_events ORDER BY sequence").fetchall()
        checkpoint = self._db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='order_events'").fetchone()
        issued = checkpoint[0] if checkpoint else 0
        retained = rows[-1][0] if rows else 0
        if type(issued) is not int or issued != retained:
            raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
        previous = _GENESIS
        for expected, (sequence, event_id, order_id, canonical, prev, digest) in enumerate(rows, 1):
            try:
                obj = json.loads(canonical)
                valid = (isinstance(obj, dict)
                         and obj.get("client_order_id") == order_id
                         and order_id in orders
                         and isinstance(event_id, str)
                         and _EVENT_ID.fullmatch(event_id)
                         and obj.get("kind") in _EVENT_KINDS
                         and _detail(obj.get("detail")) is obj["detail"]
                         and _aware(_parse_dt(obj.get("occurred_at")))
                         and event_id == sha256(canonical.encode("utf-8")).hexdigest()
                         and json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True, allow_nan=False) == canonical)
            except (ValueError, TypeError, LedgerIntegrityError):
                valid = False
            if (not valid or sequence != expected or prev != previous
                    or digest != _digest(previous, canonical)):
                raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
            previous = digest
        return orders

    def _row_view(self, row):
        (order_id, symbol, side, qty, order_type, limit, stop, tif, status,
         filled, broker_id, reason, session, mode, created, updated) = row
        limit_d = _decimal(limit)
        stop_d = _decimal(stop) if stop is not None else None
        if (not isinstance(order_id, str) or not _ORDER_ID.fullmatch(order_id)
                or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or side not in _SIDES or not _shares(qty)
                or type(filled) is not int or not 0 <= filled <= qty
                or order_type not in _ORDER_TYPES or not _money(limit_d)
                or (stop_d is not None and not _money(stop_d))
                or (side == "BUY" and stop_d is not None and stop_d >= limit_d)
                or tif not in _TIF or status not in _STATUSES
                or (broker_id is not None
                    and (not isinstance(broker_id, str) or not _ID.fullmatch(broker_id)))
                or (reason is not None
                    and (not isinstance(reason, str) or len(reason) > 256))
                or not _session_date(session) or mode != "paper"):
            raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
        return OrderView(order_id, symbol, side, qty, order_type, limit_d, stop_d,
                         tif, status, filled, broker_id, reason, session, mode,
                         _parse_dt(created), _parse_dt(updated))

    def _fill_view(self, row):
        fill_id, order_id, qty, price, fee, occurred = row
        price_d, fee_d = _decimal(price), _decimal(fee)
        if (not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                or not _shares(qty) or not _money(price_d)
                or not _money(fee_d, positive=False)):
            raise LedgerIntegrityError("LEDGER_STATE_CORRUPT")
        return FillView(fill_id, order_id, qty, price_d, fee_d, _parse_dt(occurred))

    def _order_row(self, client_order_id):
        row = self._db.execute(
            "SELECT client_order_id,symbol,side,qty,order_type,limit_price,"
            "stop_price,time_in_force,status,filled,broker_order_id,"
            "terminal_reason,session_id,mode,created_at,updated_at "
            "FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
        if row is None:
            raise ValueError("UNKNOWN_ORDER")
        return self._row_view(row)

    def _append_event(self, client_order_id, kind, detail, occurred_at):
        canonical, event_id = _canonical_event(client_order_id, kind, detail, occurred_at)
        tail = self._db.execute(
            "SELECT record_hash FROM order_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = tail[0] if tail else _GENESIS
        self._db.execute(
            "INSERT INTO order_events(event_id,client_order_id,canonical,"
            "previous_hash,record_hash) VALUES(?,?,?,?,?)",
            (event_id, client_order_id, canonical, previous,
             _digest(previous, canonical)))

    def _transition(self, view, target, now, *, kind, detail,
                    broker_order_id=None, terminal_reason=None, filled=None,
                    set_broker=False, set_reason=False):
        if target not in _TRANSITIONS[view.status]:
            raise ValueError("INVALID_TRANSITION")
        fields, values = ["status=?", "updated_at=?"], [target, _iso(now)]
        if set_broker:
            fields.append("broker_order_id=?")
            values.append(broker_order_id)
        if set_reason:
            fields.append("terminal_reason=?")
            values.append(terminal_reason)
        if filled is not None:
            fields.append("filled=?")
            values.append(filled)
        values.append(view.client_order_id)
        self._db.execute(
            f"UPDATE orders SET {', '.join(fields)} WHERE client_order_id=?",
            tuple(values))
        self._append_event(view.client_order_id, kind, detail, now)
        return self._order_row(view.client_order_id)

    # ---- mutations -----------------------------------------------------

    def create(self, client_order_id, *, symbol, side, qty, limit_price,
               stop_price=None, time_in_force="DAY", now, mode="paper"):
        """Insert a CREATED paper order record; never a venue instruction."""
        if (not isinstance(client_order_id, str)
                or not _ORDER_ID.fullmatch(client_order_id)):
            raise ValueError("INVALID_ORDER_ID")
        if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or side not in _SIDES or not _shares(qty)
                or time_in_force not in _TIF or not _money(limit_price)
                or (stop_price is not None and not _money(stop_price))
                or (side == "BUY" and stop_price is not None
                    and stop_price >= limit_price)):
            raise ValueError("INVALID_ORDER")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        if mode != "paper":
            raise ValueError("REAL_TRADING_DISABLED")
        with self._write():
            self._verify()
            row = self._db.execute(
                "SELECT symbol,side,qty,order_type,limit_price,stop_price,"
                "time_in_force FROM orders WHERE client_order_id=?",
                (client_order_id,)).fetchone()
            if row is not None:
                existing = (row[0], row[1], row[2], row[3], _decimal(row[4]),
                            _decimal(row[5]) if row[5] is not None else None, row[6])
                if existing == (symbol, side, qty, "LIMIT", limit_price,
                                stop_price, time_in_force):
                    return self._order_row(client_order_id)
                raise ValueError("CONFLICTING_ORDER_ID")
            self._db.execute(
                "INSERT INTO orders(client_order_id,symbol,side,qty,order_type,"
                "limit_price,stop_price,time_in_force,status,session_id,mode,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_order_id, symbol, side, qty, "LIMIT", str(limit_price),
                 str(stop_price) if stop_price is not None else None,
                 time_in_force, "CREATED", self._session, "paper",
                 _iso(now), _iso(now)))
            self._append_event(client_order_id, "CREATED", {
                "symbol": symbol, "side": side, "qty": qty,
                "limit_price": str(limit_price),
                "stop_price": str(stop_price) if stop_price is not None else None,
                "time_in_force": time_in_force}, now)
            return self._order_row(client_order_id)

    def mark_sent(self, client_order_id, *, now):
        """Assert the intent was handed to an adapter; ack still pending."""
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            if view.status == "SENT":
                return view
            return self._transition(view, "SENT", now, kind="SENT", detail={})

    def mark_acked(self, client_order_id, broker_order_id, *, now):
        """Record a caller-asserted venue acknowledgment and its order id."""
        if (not isinstance(broker_order_id, str)
                or not _ID.fullmatch(broker_order_id)):
            raise ValueError("INVALID_BROKER_ORDER_ID")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            if view.status == "ACKED":
                if view.broker_order_id != broker_order_id:
                    raise ValueError("CONFLICTING_BROKER_ACK")
                return view
            return self._transition(
                view, "ACKED", now, kind="ACKED",
                detail={"broker_order_id": broker_order_id},
                broker_order_id=broker_order_id, set_broker=True)

    def record_fill(self, client_order_id, fill_id, qty, price, *,
                    fee=D(0), occurred_at, now):
        """Record a caller-asserted fill; price must respect the side's limit."""
        if (not isinstance(fill_id, str) or not _ID.fullmatch(fill_id)
                or not _shares(qty) or not _money(price)
                or not _money(fee, positive=False)):
            raise ValueError("INVALID_FILL")
        if not (_aware(occurred_at) and _aware(now)):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            existing = self._db.execute(
                "SELECT client_order_id,qty,price,fee,occurred_at "
                "FROM order_fills WHERE fill_id=?", (fill_id,)).fetchone()
            if existing is not None:
                same = (existing[0] == client_order_id and existing[1] == qty
                        and _decimal(existing[2]) == price
                        and _decimal(existing[3]) == fee
                        and _parse_dt(existing[4])
                            == occurred_at.astimezone(timezone.utc))
                if not same:
                    raise ValueError("CONFLICTING_FILL_ID")
                return self._order_row(client_order_id)
            if view.status not in _FILLABLE:
                raise ValueError("INVALID_TRANSITION")
            if (view.filled + qty > view.qty
                    or (view.side == "BUY" and price > view.limit_price)
                    or (view.side == "SELL" and price < view.limit_price)):
                raise ValueError("FILL_EXCEEDS_LIMITS")
            self._db.execute(
                "INSERT INTO order_fills(fill_id,client_order_id,qty,price,fee,"
                "occurred_at) VALUES(?,?,?,?,?,?)",
                (fill_id, client_order_id, qty, str(price), str(fee),
                 _iso(occurred_at)))
            detail = {"fill_id": fill_id, "qty": qty, "price": str(price),
                      "fee": str(fee), "occurred_at": _iso(occurred_at)}
            filled = view.filled + qty
            if filled == view.qty:
                return self._transition(view, "FILLED", now, kind="FILL",
                                        detail=detail, filled=filled)
            if view.status == "CANCEL_PENDING":
                # A partial fill racing an in-flight cancel stays pending.
                self._db.execute(
                    "UPDATE orders SET filled=?,updated_at=? "
                    "WHERE client_order_id=?", (filled, _iso(now), client_order_id))
                self._append_event(client_order_id, "FILL", detail, now)
                return self._order_row(client_order_id)
            return self._transition(view, "PARTIALLY_FILLED", now, kind="FILL",
                                    detail=detail, filled=filled)

    def request_cancel(self, client_order_id, *, now):
        """CREATED cancels locally; ACKED/PARTIAL moves to CANCEL_PENDING."""
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            if view.status in ("CANCEL_PENDING", "CANCELLED"):
                return view
            if view.status == "CREATED":
                return self._transition(view, "CANCELLED", now, kind="CANCELLED",
                                        detail={"venue": "local"})
            return self._transition(view, "CANCEL_PENDING", now,
                                    kind="CANCEL_REQUESTED", detail={})

    def mark_cancelled(self, client_order_id, *, now, confirmed_absent=False):
        """Terminal cancellation; from SENT requires reconcile-confirmed absence."""
        if type(confirmed_absent) is not bool:
            raise ValueError("INVALID_CONFIRMATION")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            if view.status == "CANCELLED":
                return view
            if view.status == "SENT" and not confirmed_absent:
                raise ValueError("UNCONFIRMED_LOST_ACK")
            return self._transition(
                view, "CANCELLED", now, kind="CANCELLED",
                detail={"confirmed_absent": confirmed_absent})

    def mark_rejected(self, client_order_id, reason, *, now):
        """Terminal venue or pre-dispatch rejection with an auditable reason."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise ValueError("INVALID_REASON")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            if view.status == "REJECTED":
                if view.terminal_reason != reason:
                    raise ValueError("CONFLICTING_TERMINAL_REASON")
                return view
            return self._transition(view, "REJECTED", now, kind="REJECTED",
                                    detail={"reason": reason},
                                    terminal_reason=reason, set_reason=True)

    def mark_expired(self, client_order_id, *, now):
        """Terminal session expiry for orders that reached the venue."""
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        with self._write():
            self._verify()
            view = self._order_row(client_order_id)
            if view.status == "EXPIRED":
                return view
            return self._transition(view, "EXPIRED", now, kind="EXPIRED", detail={})

    # ---- queries ---------------------------------------------------------

    def get(self, client_order_id):
        return self._select_one(client_order_id)

    def orders(self):
        return self._select(None)

    def open_orders(self):
        return self._select(_OPEN)

    def pending_reconcile(self):
        """Orders with unresolved venue state: sent-but-unacked, cancel pending."""
        return self._select(frozenset({"SENT", "CANCEL_PENDING"}))

    def _select_one(self, client_order_id):
        self._db.execute("BEGIN")
        try:
            self._verify()
            row = self._db.execute(
                "SELECT client_order_id FROM orders WHERE client_order_id=?",
                (client_order_id,)).fetchone()
            view = self._order_row(client_order_id) if row else None
            self._db.execute("COMMIT")
            return view
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _select(self, statuses):
        self._db.execute("BEGIN")
        try:
            self._verify()
            rows = self._db.execute(
                "SELECT client_order_id,symbol,side,qty,order_type,limit_price,"
                "stop_price,time_in_force,status,filled,broker_order_id,"
                "terminal_reason,session_id,mode,created_at,updated_at "
                "FROM orders ORDER BY created_at, client_order_id").fetchall()
            views = tuple(v for v in (self._row_view(r) for r in rows)
                          if statuses is None or v.status in statuses)
            self._db.execute("COMMIT")
            return views
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def fills(self, client_order_id):
        self._db.execute("BEGIN")
        try:
            self._verify()
            rows = self._db.execute(
                "SELECT fill_id,client_order_id,qty,price,fee,occurred_at "
                "FROM order_fills WHERE client_order_id=? "
                "ORDER BY occurred_at, fill_id", (client_order_id,)).fetchall()
            views = tuple(self._fill_view(r) for r in rows)
            self._db.execute("COMMIT")
            return views
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
