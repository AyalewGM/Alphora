"""Crash-recoverable journal for *caller-invented* offline SELL receipts.

This is a local synthetic accounting aid, not a broker adapter, exchange
simulator, order router, or source of authenticated fills. A separately
reconciled opening snapshot remains mandatory. No network imports.
"""
from contextlib import contextmanager
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
import sqlite3

from .offline_exit_projection import (
    PositionCost, SyntheticSellReceipt, project_synthetic_sells,
)


class OfflineSellJournal:
    """Append and replay synthetic receipts atomically, with stable idempotency."""

    def __init__(self, path, *, session_id: str, opening_cash: D,
                 opening_positions: tuple[PositionCost, ...]):
        if (not isinstance(path, (str, Path)) or not str(path).strip()
                or str(path) == ":memory:" or str(path).lower().startswith("file:")
                or "\x00" in str(path)):
            raise ValueError("INVALID_JOURNAL_PATH")
        # Validate the externally reconciled opening book before opening a DB.
        initial = project_synthetic_sells(session_id, opening_cash, opening_positions, ())
        self._session = session_id
        self._opening_cash = opening_cash
        self._opening_positions = opening_positions
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS sell_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS synthetic_sells "
                    "(receipt_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, "
                    "symbol TEXT NOT NULL, shares INTEGER NOT NULL, "
                    "price TEXT NOT NULL, fee TEXT NOT NULL)")
                meta = dict(self._db.execute("SELECT key, value FROM sell_meta"))
                expected = {
                    "schema": "1", "session_id": session_id,
                    "opening_sha256": initial.receipt_sha256,
                }
                if not meta:
                    if self._db.execute(
                        "SELECT 1 FROM synthetic_sells LIMIT 1"
                    ).fetchone():
                        raise ValueError("SELL_JOURNAL_CORRUPT")
                    self._db.executemany(
                        "INSERT INTO sell_meta(key,value) VALUES (?,?)",
                        expected.items())
                elif meta != expected:
                    raise ValueError("SELL_JOURNAL_OPENING_MISMATCH")
                self._replay()
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _read_receipts(self):
        rows = self._db.execute(
            "SELECT receipt_id, sequence, symbol, shares, price, fee "
            "FROM synthetic_sells ORDER BY sequence").fetchall()
        receipts = []
        try:
            for ident, sequence, symbol, shares, price, fee in rows:
                receipts.append(SyntheticSellReceipt(
                    ident, sequence, symbol, shares, D(price), D(fee)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("SELL_JOURNAL_CORRUPT") from exc
        return tuple(receipts)

    def _replay(self):
        # Check metadata again on every operation; avoid continuing on a
        # partially modified or externally corrupted journal.
        meta = dict(self._db.execute("SELECT key, value FROM sell_meta"))
        initial = project_synthetic_sells(
            self._session, self._opening_cash, self._opening_positions, ())
        if meta != {"schema": "1", "session_id": self._session,
                    "opening_sha256": initial.receipt_sha256}:
            raise ValueError("SELL_JOURNAL_CORRUPT")
        receipts = self._read_receipts()
        try:
            result = project_synthetic_sells(
                self._session, self._opening_cash, self._opening_positions,
                receipts)
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("SELL_JOURNAL_CORRUPT") from exc
        return receipts, result

    def snapshot(self):
        self._db.execute("BEGIN")
        try:
            _, result = self._replay()
            self._db.execute("COMMIT")
            return result
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def append(self, receipt: SyntheticSellReceipt):
        """Record one hypothetical SELL receipt; idempotent across restarts.

        No inferred market fills or orders: the caller fabricates receipts.
        A rejected transaction cannot alter persisted cash or positions.
        """
        if not isinstance(receipt, SyntheticSellReceipt):
            raise ValueError("INVALID_SYNTHETIC_SELL_RECEIPT")
        with self._transaction():
            existing, projection = self._replay()
            for prior in existing:
                if prior.receipt_id == receipt.receipt_id:
                    if prior == receipt:
                        return projection
                    raise ValueError("CONFLICTING_SELL_RECEIPT_ID")
            # Validate before insertion, including contiguous sequence,
            # available holdings and fee-inclusive cash/basis conservation.
            proposed = project_synthetic_sells(
                self._session, self._opening_cash, self._opening_positions,
                existing + (receipt,))
            self._db.execute(
                "INSERT INTO synthetic_sells"
                "(receipt_id,sequence,symbol,shares,price,fee) "
                "VALUES(?,?,?,?,?,?)",
                (receipt.receipt_id, receipt.sequence, receipt.symbol,
                 receipt.shares, str(receipt.price), str(receipt.fee)))
            return proposed

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
