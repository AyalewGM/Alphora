"""Synthetic P0 contract tests, not approval of live or unattended runner."""
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D, localcontext
from datetime import date, timedelta
from pathlib import Path

from alphora.session_equity import SessionEquityCheckpoint

A = "a" * 64
B = "b" * 64
C = "c" * 64


class SessionEquityCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = str(Path(self.tmp.name) / "checkpoint.sqlite")
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.store = SessionEquityCheckpoint(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def initial(self):
        return self.store.bootstrap("2026-11-02", D("100000"), A, proven_new_book=True)

    def reach_next_open(self, target):
        """Reach next opening equity without ever violating a prior daily 2% halt."""
        self.initial()
        closes = (["105000"] if target == "105000" else
                  ["98500", "97000", "95600", "95000"])
        for close in closes:
            prior = self.store.snapshot()
            result = self.mark(close)
            self.assertFalse(result.halted)
            session = (date.fromisoformat(prior.session_id) + timedelta(days=1)).isoformat()
            following = self.store.rollover(
                previous_session=prior.session_id, next_session=session,
                opening_equity=D(close), opening_receipt=C,
                expected_revision=result.revision, reconciled=True,
                zero_pending=True, verified_calendar=True)
            self.assertEqual(following.opening_equity, D(close))
        self.assertEqual(self.store.snapshot().opening_equity, D(target))

    def mark(self, equity, receipt=B):
        return self.store.observe(session_id=self.store.snapshot().session_id,
                                  equity=D(equity), receipt=receipt,
                                  expected_revision=self.store.snapshot().revision,
                                  reconciled=True, marks_verified=True)

    def rollover(self, opening, receipt=C):
        old = self.store.snapshot()
        return self.store.rollover(previous_session=old.session_id,
                                   next_session="2026-11-03",
                                   opening_equity=D(opening), opening_receipt=receipt,
                                   expected_revision=old.revision,
                                   reconciled=True, zero_pending=True,
                                   verified_calendar=True)

    def test_pristine_only_and_deleted_row_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "FRESH_BOOK_EVIDENCE_REQUIRED"):
            self.store.bootstrap("2026-11-02", D("100000"), A)
        self.initial()
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_EXISTS"):
            self.initial()
        self.conn.execute("DELETE FROM session_equity_checkpoint")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_EXISTS"):
            self.initial()

    def test_exact_105k_and_95k_boundaries_with_cents(self):
        for opening, above, floor, down in (("105000", "102900.01", "102900.00", "102000.00"),
                                            ("95000", "93100.01", "93100.00", "93099.99")):
            with self.subTest(opening=opening):
                self.conn.close()
                self.conn = sqlite3.connect(":memory:", isolation_level=None)
                self.store = SessionEquityCheckpoint(self.conn)
                self.reach_next_open(opening)
                with localcontext() as ctx:
                    ctx.prec = 3
                    self.assertFalse(self.mark(above).halted)
                    self.assertTrue(self.mark(floor, C).halted)
                    self.assertTrue(self.mark(down, "d" * 64).halted)
                    self.assertTrue(self.mark(opening, "e" * 64).halted)

    def test_fee_inclusive_observation_and_persistent_restart(self):
        self.reach_next_open("105000")
        self.assertFalse(self.mark("102900.01").halted)
        # $0.02 additional simulated fees bring marked equity below the floor.
        self.assertTrue(self.mark("102899.99", C).halted)
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        self.assertTrue(self.store.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PERSISTED_SAFETY_HALT"):
            old = self.store.snapshot()
            self.store.rollover(previous_session=old.session_id,
                                next_session="2026-11-04",
                                opening_equity=D("102899.99"),
                                opening_receipt=C, expected_revision=old.revision,
                                reconciled=True, zero_pending=True,
                                verified_calendar=True)

    def test_rollover_requires_final_verified_mark_and_no_pending(self):
        self.initial()
        with self.assertRaisesRegex(ValueError, "ROLLOVER_NOT_VERIFIED"):
            self.rollover("100000")
        self.mark("105000")
        old = self.store.snapshot()
        for kwargs in ({"zero_pending": False}, {"reconciled": False},
                       {"verified_calendar": False}):
            with self.subTest(kwargs=kwargs):
                params = dict(previous_session=old.session_id,
                              next_session="2026-11-03", opening_equity=D("105000"),
                              opening_receipt=C, expected_revision=old.revision,
                              reconciled=True, zero_pending=True, verified_calendar=True)
                params.update(kwargs)
                with self.assertRaisesRegex(ValueError, "ROLLOVER_NOT_VERIFIED"):
                    self.store.rollover(**params)
        with self.assertRaisesRegex(ValueError, "OPENING_EQUITY_MISMATCH"):
            self.rollover("104999.99")
        next_state = self.rollover("105000")
        self.assertEqual(next_state.opening_equity, D("105000"))
        self.assertEqual(next_state.revision, old.revision + 1)
        self.assertEqual(next_state.observations, 0)
        self.assertEqual(self.rollover("105000"), next_state)  # duplicate exact idempotent
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SESSION_REPLAY"):
            self.rollover("105000", "d" * 64)
        self.assertTrue(self.mark("102000").halted)

    def test_stale_revision_replay_and_bad_provenance_fail_closed(self):
        self.initial()
        old = self.store.snapshot()
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_SESSION_MARK"):
            self.store.observe(session_id=old.session_id, equity=D("95000"),
                               receipt=B, expected_revision=old.revision, reconciled=False,
                               marks_verified=True)
        self.assertEqual(self.store.snapshot(), old)
        after = self.mark("99000")
        self.assertEqual(self.mark("99000"), after)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SESSION_MARK_REPLAY"):
            self.mark("99000.01")
        with self.assertRaisesRegex(ValueError, "STALE_SESSION_REVISION"):
            self.store.observe(session_id=old.session_id, equity=D("98000"),
                               receipt=C, expected_revision=old.revision,
                               reconciled=True, marks_verified=True)
        self.assertEqual(self.store.snapshot(), after)

    def test_corruption_and_same_transaction_atomic_rollback(self):
        self.initial()
        before = self.store.snapshot()
        self.conn.execute("BEGIN IMMEDIATE")
        self.store.observe(session_id=before.session_id, equity=D("98000"),
                           receipt=B, expected_revision=before.revision,
                           reconciled=True, marks_verified=True)
        self.assertTrue(self.store.snapshot().halted)
        self.conn.execute("ROLLBACK")  # same DB transaction as eventual OMS book
        self.assertEqual(self.store.snapshot(), before)
        self.conn.execute("UPDATE session_equity_checkpoint SET opening_equity='nan'")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()

    def test_rejects_unobserved_equity_tampering_even_without_mark_receipt(self):
        before = self.initial()
        for value in ("98000.00", "90000.00"):
            with self.subTest(persisted_last=value):
                self.conn.execute("UPDATE session_equity_checkpoint SET last_equity=?",
                                  (value,))
                with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
                    self.store.snapshot()
                self.conn.execute("UPDATE session_equity_checkpoint SET last_equity=?",
                                  (str(before.last_equity),))
        self.assertEqual(self.store.snapshot(), before)

    def test_rejects_unhalted_observed_loss_at_inclusive_floor(self):
        self.initial()
        valid = self.mark("99000.00")
        for value, valid_unhalted in (("98000.01", True),
                                      ("98000.00", False),
                                      ("97999.99", False),
                                      ("90000", False)):
            with self.subTest(persisted_last=value):
                self.conn.execute("UPDATE session_equity_checkpoint "
                                  "SET last_equity=?, halted=0", (value,))
                if valid_unhalted:
                    self.assertFalse(self.store.snapshot().halted)
                else:
                    with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
                        self.store.snapshot()
                self.conn.execute("UPDATE session_equity_checkpoint "
                                  "SET last_equity=?, halted=0",
                                  (str(valid.last_equity),))
        self.assertEqual(self.store.snapshot(), valid)
        # Existing one-way halt may remain true after a later rebound.
        self.assertTrue(self.store.halt().halted)
        self.conn.execute("UPDATE session_equity_checkpoint SET last_equity='90000'")
        self.assertTrue(self.store.snapshot().halted)

    def test_rejects_unbounded_persisted_numbers_and_impossible_revision(self):
        self.initial()
        for invalid in ("9" * 65, "1E+999999999", "not-a-decimal"):
            with self.subTest(invalid=invalid[:24]):
                self.conn.execute("UPDATE session_equity_checkpoint "
                                  "SET opening_equity=?", (invalid,))
                with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
                    self.store.snapshot()
                self.conn.execute("UPDATE session_equity_checkpoint "
                                  "SET opening_equity='100000'")
        self.mark("99000")
        self.conn.execute("UPDATE session_equity_checkpoint SET revision=0")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()

    def test_strict_decimal_and_session_id_input(self):
        for value in (D("1.001"), D("NaN"), -D("1"), 100000):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "INVALID_SESSION_EQUITY"):
                    self.store.bootstrap("2026-11-02", value, A, proven_new_book=True)
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_STARTING_EQUITY"):
            self.store.bootstrap("2026-11-02", D("95000"), A, proven_new_book=True)
        for sid in ("2026-11-31", "2026-11-2", "2026-11-03T00:00:00Z"):
            with self.subTest(sid=sid):
                with self.assertRaisesRegex(ValueError, "INVALID_EXCHANGE_SESSION"):
                    self.store.bootstrap(sid, D("100000"), A, proven_new_book=True)


if __name__ == "__main__":
    unittest.main()
