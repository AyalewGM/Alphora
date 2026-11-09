"""Offline order-ledger lifecycle, idempotency, integrity and restart tests."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from alphora.orders import OrderLedger, LedgerIntegrityError, _configure_wal

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


class OrderLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper-orders.sqlite"
        self.ledger = OrderLedger(self.path, session_id="2026-11-02")
        self.oid = "a" * 64

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def reopen(self):
        self.ledger.close()
        self.ledger = OrderLedger(self.path, session_id="2026-11-02")

    def create(self, oid=None, **kw):
        args = dict(symbol="SPY", side="BUY", qty=100, limit_price=D("50"),
                    stop_price=D("48"), now=at())
        args.update(kw)
        return self.ledger.create(oid or self.oid, **args)

    def test_requires_durable_path_and_valid_session(self):
        for bad in ("", "  ", ":memory:", "file::memory:?cache=shared"):
            with self.subTest(path=bad):
                with self.assertRaisesRegex(ValueError, "DURABLE_PATH_REQUIRED"):
                    OrderLedger(bad, session_id="2026-11-02")
        for bad in ("nope", "2026-99-99", 2026):
            with self.subTest(session=bad):
                with self.assertRaisesRegex(ValueError, "INVALID_SESSION_ID"):
                    OrderLedger(Path(self.tmp.name) / f"s{bad}.sqlite",
                                session_id=bad)

    def test_create_validation_fails_closed(self):
        base = dict(symbol="SPY", side="BUY", qty=100, limit_price=D("50"),
                    now=at())
        invalid = (
            ("short-id", dict(base)),
            (self.oid, {**base, "symbol": "spy;drop"}),
            (self.oid, {**base, "side": "HOLD"}),
            (self.oid, {**base, "qty": 0}),
            (self.oid, {**base, "qty": 1.5}),
            (self.oid, {**base, "limit_price": D("0")}),
            (self.oid, {**base, "limit_price": D("nan")}),
            (self.oid, {**base, "stop_price": D("51")}),
            (self.oid, {**base, "time_in_force": "GTC"}),
            (self.oid, {**base, "now": datetime(2026, 11, 2)}),
            (self.oid, {**base, "mode": "live"}),
        )
        for oid, kw in invalid:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    self.ledger.create(oid, **kw)
        self.assertEqual(self.ledger.orders(), ())
        self.assertFalse(hasattr(self.ledger, "submit_order"))

    def test_create_is_idempotent_and_conflicts_fail(self):
        view = self.create()
        self.assertEqual(view.status, "CREATED")
        self.assertEqual(view.mode, "paper")
        self.assertEqual(self.create(), view)  # same key, same fields: retry
        with self.assertRaisesRegex(ValueError, "CONFLICTING_ORDER_ID"):
            self.create(qty=90)
        self.assertEqual(len(self.ledger.orders()), 1)

    def test_happy_path_buy_lifecycle(self):
        self.create()
        sent = self.ledger.mark_sent(self.oid, now=at(1))
        self.assertEqual(sent.status, "SENT")
        self.assertIsNone(sent.broker_order_id)
        self.assertEqual(self.ledger.mark_sent(self.oid, now=at(2)), sent)
        acked = self.ledger.mark_acked(self.oid, "brk-1", now=at(3))
        self.assertEqual(acked.status, "ACKED")
        self.assertEqual(acked.broker_order_id, "brk-1")
        self.assertEqual(self.ledger.mark_acked(self.oid, "brk-1", now=at(4)),
                         acked)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_BROKER_ACK"):
            self.ledger.mark_acked(self.oid, "brk-2", now=at(5))
        partial = self.ledger.record_fill(self.oid, "f1", 40, D("49"),
                                          fee=D("1"), occurred_at=at(6),
                                          now=at(6))
        self.assertEqual((partial.status, partial.filled),
                         ("PARTIALLY_FILLED", 40))
        filled = self.ledger.record_fill(self.oid, "f2", 60, D("50"),
                                         occurred_at=at(7), now=at(7))
        self.assertEqual((filled.status, filled.filled), ("FILLED", 100))
        self.assertEqual([f.fill_id for f in self.ledger.fills(self.oid)],
                         ["f1", "f2"])
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.mark_expired(self.oid, now=at(8))

    def test_sell_fill_must_respect_minimum_limit(self):
        self.create(symbol="QQQ", side="SELL", qty=10, limit_price=D("100"),
                    stop_price=None)
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.mark_acked(self.oid, "brk", now=at(2))
        with self.assertRaisesRegex(ValueError, "FILL_EXCEEDS_LIMITS"):
            self.ledger.record_fill(self.oid, "f1", 5, D("99.99"),
                                    occurred_at=at(3), now=at(3))
        view = self.ledger.record_fill(self.oid, "f1", 10, D("100.01"),
                                       occurred_at=at(4), now=at(4))
        self.assertEqual(view.status, "FILLED")

    def test_transition_ordering_is_enforced(self):
        self.create()
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.mark_acked(self.oid, "brk", now=at(1))
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.record_fill(self.oid, "f", 1, D("50"),
                                    occurred_at=at(1), now=at(1))
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.mark_expired(self.oid, now=at(1))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_ORDER"):
            self.ledger.mark_sent("b" * 64, now=at(1))

    def test_created_cancels_locally_without_venue(self):
        self.create()
        view = self.ledger.request_cancel(self.oid, now=at(1))
        self.assertEqual(view.status, "CANCELLED")
        self.assertIsNone(view.broker_order_id)
        self.assertEqual(self.ledger.request_cancel(self.oid, now=at(2)), view)
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.mark_sent(self.oid, now=at(3))

    def test_acked_cancel_flow_and_fill_race(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.mark_acked(self.oid, "brk", now=at(2))
        self.ledger.record_fill(self.oid, "f1", 40, D("50"),
                                occurred_at=at(3), now=at(3))
        pending = self.ledger.request_cancel(self.oid, now=at(4))
        self.assertEqual(pending.status, "CANCEL_PENDING")
        self.assertIn(self.oid,
                      [o.client_order_id for o in self.ledger.pending_reconcile()])
        # A partial fill racing the in-flight cancel stays pending.
        raced = self.ledger.record_fill(self.oid, "f2", 10, D("50"),
                                        occurred_at=at(5), now=at(5))
        self.assertEqual((raced.status, raced.filled), ("CANCEL_PENDING", 50))
        done = self.ledger.mark_cancelled(self.oid, now=at(6))
        self.assertEqual((done.status, done.filled), ("CANCELLED", 50))
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.record_fill(self.oid, "f3", 1, D("50"),
                                    occurred_at=at(7), now=at(7))

    def test_fill_completing_during_cancel_lands_filled(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.mark_acked(self.oid, "brk", now=at(2))
        self.ledger.request_cancel(self.oid, now=at(3))
        view = self.ledger.record_fill(self.oid, "f1", 100, D("50"),
                                       occurred_at=at(4), now=at(4))
        self.assertEqual(view.status, "FILLED")

    def test_sent_lost_ack_resolves_only_with_confirmation(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.assertEqual([o.status for o in self.ledger.pending_reconcile()],
                         ["SENT"])
        with self.assertRaisesRegex(ValueError, "UNCONFIRMED_LOST_ACK"):
            self.ledger.mark_cancelled(self.oid, now=at(2))
        view = self.ledger.mark_cancelled(self.oid, now=at(3),
                                          confirmed_absent=True)
        self.assertEqual(view.status, "CANCELLED")
        self.assertEqual(self.ledger.pending_reconcile(), ())

    def test_rejection_paths_and_reason_conflict(self):
        self.create()
        rejected = self.ledger.mark_rejected(self.oid, "RISK_VETO", now=at(1))
        self.assertEqual((rejected.status, rejected.terminal_reason),
                         ("REJECTED", "RISK_VETO"))
        self.assertEqual(
            self.ledger.mark_rejected(self.oid, "RISK_VETO", now=at(2)),
            rejected)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_TERMINAL_REASON"):
            self.ledger.mark_rejected(self.oid, "OTHER", now=at(3))
        other = "c" * 64
        self.create(other)
        self.ledger.mark_sent(other, now=at(4))
        view = self.ledger.mark_rejected(other, "VENUE_REJECT", now=at(5))
        self.assertEqual(view.status, "REJECTED")

    def test_expiry_only_after_venue_ack(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        with self.assertRaisesRegex(ValueError, "INVALID_TRANSITION"):
            self.ledger.mark_expired(self.oid, now=at(2))
        self.ledger.mark_acked(self.oid, "brk", now=at(3))
        view = self.ledger.mark_expired(self.oid, now=at(4))
        self.assertEqual(view.status, "EXPIRED")
        self.assertEqual(self.ledger.mark_expired(self.oid, now=at(5)), view)

    def test_fill_bounds_dedupe_and_conflicts(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.mark_acked(self.oid, "brk", now=at(2))
        for qty, price, fee in ((101, D("50"), D(0)), (1, D("50.01"), D(0)),
                                (1, D("0"), D(0)), (1, D("50"), D("-1"))):
            with self.subTest(qty=qty, price=price, fee=fee):
                with self.assertRaises(ValueError):
                    self.ledger.record_fill(self.oid, "fx", qty, price, fee=fee,
                                            occurred_at=at(3), now=at(3))
        view = self.ledger.record_fill(self.oid, "fx", 50, D("50"),
                                       occurred_at=at(3), now=at(3))
        self.assertEqual(self.ledger.record_fill(
            self.oid, "fx", 50, D("50"), occurred_at=at(3), now=at(4)), view)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_FILL_ID"):
            self.ledger.record_fill(self.oid, "fx", 50, D("49"),
                                    occurred_at=at(3), now=at(5))
        other = "d" * 64
        self.create(other, symbol="QQQ", qty=1, limit_price=D("10"),
                    stop_price=None)
        self.ledger.mark_sent(other, now=at(7))
        self.ledger.mark_acked(other, "brk2", now=at(8))
        with self.assertRaisesRegex(ValueError, "CONFLICTING_FILL_ID"):
            self.ledger.record_fill(other, "fx", 1, D("10"),
                                    occurred_at=at(9), now=at(9))

    def test_restart_persists_state_and_detects_wrong_session(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.mark_acked(self.oid, "brk", now=at(2))
        self.ledger.record_fill(self.oid, "f1", 10, D("50"),
                                occurred_at=at(3), now=at(3))
        before = self.ledger.get(self.oid)
        self.reopen()
        self.assertEqual(self.ledger.get(self.oid), before)
        self.assertEqual([f.fill_id for f in self.ledger.fills(self.oid)],
                         ["f1"])
        self.ledger.close()
        with self.assertRaisesRegex(ValueError, "LEDGER_REOPEN_MISMATCH"):
            OrderLedger(self.path, session_id="2026-11-03")
        self.ledger = OrderLedger(self.path, session_id="2026-11-02")

    def test_corrupt_order_row_fails_closed_on_reopen(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE orders SET status='FILLED' "
                         "WHERE client_order_id=?", (self.oid,))
        with self.assertRaises(LedgerIntegrityError):
            OrderLedger(self.path, session_id="2026-11-02")
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE orders SET status='SENT' "
                         "WHERE client_order_id=?", (self.oid,))
        self.ledger = OrderLedger(self.path, session_id="2026-11-02")
        self.assertEqual(self.ledger.get(self.oid).status, "SENT")

    def test_corrupt_cash_field_and_fill_sum_detected(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.mark_acked(self.oid, "brk", now=at(2))
        self.ledger.record_fill(self.oid, "f1", 10, D("50"),
                                occurred_at=at(3), now=at(3))
        self.ledger.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE orders SET filled=5 "
                         "WHERE client_order_id=?", (self.oid,))
        with self.assertRaises(LedgerIntegrityError):
            OrderLedger(self.path, session_id="2026-11-02")
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE orders SET filled=10 "
                         "WHERE client_order_id=?", (self.oid,))
        self.ledger = OrderLedger(self.path, session_id="2026-11-02")

    def test_event_chain_tamper_detected_on_reopen(self):
        self.create()
        self.ledger.mark_sent(self.oid, now=at(1))
        self.ledger.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE order_events SET canonical='{}' "
                         "WHERE sequence=2")
        with self.assertRaises(LedgerIntegrityError):
            OrderLedger(self.path, session_id="2026-11-02")

    def test_event_tail_truncation_detected(self):
        path = Path(self.tmp.name) / "trunc.sqlite"
        ledger = OrderLedger(path, session_id="2026-11-02")
        ledger.create("e" * 64, symbol="SPY", side="BUY", qty=1,
                      limit_price=D("10"), now=at())
        ledger.close()
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM order_events WHERE sequence=1")
        with self.assertRaises(LedgerIntegrityError):
            OrderLedger(path, session_id="2026-11-02")

    def test_abrupt_child_exit_discards_uncommitted_mutation(self):
        self.create()
        before = self.ledger.get(self.oid)
        self.ledger.close()
        script = """
import os
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1], timeout=5, isolation_level=None)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE orders SET status='FILLED'")
os._exit(23)
"""
        child = subprocess.run([sys.executable, "-c", script, str(self.path)],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.ledger = OrderLedger(self.path, session_id="2026-11-02")
        self.assertEqual(self.ledger.get(self.oid), before)

    def test_wal_setup_retries_only_transient_sqlite_lock_errors(self):
        class FakeDb:
            calls = 0
            error_code = sqlite3.SQLITE_BUSY

            def execute(self, sql):
                self.calls += 1
                self.last_sql = sql
                if self.calls == 1:
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = self.error_code
                    raise error
                return self

            def fetchone(self):
                return ("wal",)

        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            with self.subTest(code=code):
                db = FakeDb()
                db.error_code = code
                _configure_wal(db)
                self.assertEqual(db.calls, 2)
                self.assertEqual(db.last_sql, "PRAGMA journal_mode=WAL")

        # Other database failures must not be retried or misclassified.
        db = FakeDb()
        db.error_code = sqlite3.SQLITE_IOERR
        with self.assertRaises(sqlite3.OperationalError):
            _configure_wal(db)
        self.assertEqual(db.calls, 1)

        # A successful PRAGMA that did not enable WAL must fail closed.
        db = FakeDb()
        db.calls = 1
        db.fetchone = lambda: ("delete",)
        with self.assertRaisesRegex(LedgerIntegrityError, "WAL_MODE_REQUIRED"):
            _configure_wal(db)

    def test_wal_retry_deadline_and_malformed_pragma_fail_closed(self):
        class AlwaysBusy:
            calls = 0

            def execute(self, sql):
                self.calls += 1
                self.last_sql = sql
                error = sqlite3.OperationalError("database is locked")
                # Extended codes must be classified by their base SQLite code.
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (1 << 8)
                raise error

        db = AlwaysBusy()
        with patch("alphora.orders.monotonic", side_effect=[10.0, 16.0]):
            with patch("alphora.orders.sleep") as pause:
                with self.assertRaises(sqlite3.OperationalError):
                    _configure_wal(db)
                pause.assert_not_called()
        self.assertEqual(db.calls, 1)

        # Bound the final backoff to the time remaining in the retry window.
        db = AlwaysBusy()
        with patch("alphora.orders.monotonic", side_effect=[10.0, 14.995, 16.0]):
            with patch("alphora.orders.sleep") as pause:
                with self.assertRaises(sqlite3.OperationalError):
                    _configure_wal(db)
                pause.assert_called_once()
                self.assertAlmostEqual(pause.call_args.args[0], 0.005, places=6)
        self.assertEqual(db.calls, 2)

        class MalformedPragma:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            def execute(self, sql):
                self.calls += 1
                return self

            def fetchone(self):
                return self.result

        for result in (None, (), (None,), (1,), ("delete",), ("wal", "extra")):
            with self.subTest(result=result):
                db = MalformedPragma(result)
                with self.assertRaisesRegex(LedgerIntegrityError, "WAL_MODE_REQUIRED"):
                    _configure_wal(db)
                self.assertEqual(db.calls, 1)

    def test_concurrent_create_serializes_to_one_record(self):
        # Repeat on fresh databases to exercise simultaneous WAL initialization
        # and idempotent order creation across independent connection waves.
        self.ledger.close()
        try:
            for wave in range(3):
                path = Path(self.tmp.name) / f"concurrent-{wave}.sqlite"
                # Release the first eight tasks together, rather than relying
                # on thread scheduling to exercise a simultaneous WAL setup.
                gate = Barrier(8)

                def attempt(index):
                    if index < 8:
                        gate.wait(timeout=15)
                    ledger = OrderLedger(path, session_id="2026-11-02")
                    try:
                        return ledger.create(
                            "f" * 64, symbol="SPY", side="BUY", qty=10,
                            limit_price=D("50"), now=at()
                        ).status
                    finally:
                        ledger.close()

                with self.subTest(wave=wave):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        statuses = list(pool.map(attempt, range(12)))
                    self.assertEqual(statuses.count("CREATED"), 12)
                    ledger = OrderLedger(path, session_id="2026-11-02")
                    try:
                        self.assertEqual(len(ledger.orders()), 1)
                        with sqlite3.connect(path) as conn:
                            self.assertEqual(conn.execute(
                                "SELECT COUNT(*) FROM order_events"
                            ).fetchone()[0], 1)
                    finally:
                        ledger.close()
        finally:
            self.ledger = OrderLedger(self.path, session_id="2026-11-02")


if __name__ == "__main__":
    unittest.main()
