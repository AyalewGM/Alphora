"""Synthetic P0 contract tests, not approval of live or unattended runner."""
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D, localcontext
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

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

    def test_nonadjacent_mark_replay_is_idempotent_after_restart(self):
        self.initial()
        initial = self.store.snapshot()
        first = self.mark("100000", B)
        second = self.mark("99500", C)
        self.assertEqual(first.observations, 1)
        self.assertEqual(second.observations, 2)
        self.assertEqual(second.revision, initial.revision + 2)
        # B -> C -> B must not replace C's newer $99,500 observation.
        self.assertEqual(self.mark("100000", B), second)
        self.assertEqual(self.store.snapshot(), second)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SESSION_MARK_REPLAY"):
            self.mark("100000.01", B)
        self.assertEqual(self.store.snapshot(), second)
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        self.assertEqual(self.mark("100000", B), second)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SESSION_MARK_REPLAY"):
            self.mark("100000.01", B)
        self.assertEqual(self.store.snapshot(), second)
        # Durable receipts cannot be rewritten or removed via normal SQL.
        for statement in (
            "UPDATE session_equity_marks SET equity='100000' WHERE receipt=?",
            "DELETE FROM session_equity_marks WHERE receipt=?",
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            "IMMUTABLE_SESSION_MARK"):
                    self.conn.execute(statement, (C,))
        third = self.mark("99000", "d" * 64)
        self.assertEqual(third.observations, 3)
        self.assertEqual(third.revision, second.revision + 1)

    def test_rejects_missing_or_contradictory_mark_history(self):
        self.initial()
        self.mark("99000", B)
        self.conn.execute("UPDATE session_equity_checkpoint SET last_equity='99000.01'")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        self.conn.execute("UPDATE session_equity_checkpoint SET last_equity='99000'")
        self.conn.execute("DROP TABLE session_equity_marks")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.mark("98000", C)

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
        for value in ("98000.01", "98000.00", "97999.99", "90000"):
            with self.subTest(persisted_last=value):
                self.conn.execute("UPDATE session_equity_checkpoint "
                                  "SET last_equity=?, halted=0", (value,))
                with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
                    self.store.snapshot()
                self.conn.execute("UPDATE session_equity_checkpoint "
                                  "SET last_equity=?, halted=0",
                                  (str(valid.last_equity),))
        self.assertEqual(self.store.snapshot(), valid)
        # Existing one-way halt may remain true after a later rebound.
        self.assertTrue(self.store.halt().halted)
        self.assertTrue(self.store.snapshot().halted)
        self.conn.execute("UPDATE session_equity_checkpoint SET last_equity='90000'")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()

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

    def test_failed_checkpoint_ops_do_not_leak_into_outer_book_commit(self):
        # A caller can recover from a checkpoint exception and still commit
        # unrelated book work. No half-created checkpoint or unsafe halt may
        # leak from the failed operation into that outer transaction.
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("CREATE TABLE unrelated (value INTEGER)")
        with patch.object(self.store, "_read", side_effect=RuntimeError("injected read failure")):
            with self.assertRaisesRegex(RuntimeError, "injected read failure"):
                self.initial()  # failure after CREATE TABLE and INSERT
        self.assertTrue(self.conn.in_transaction)
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='session_equity_checkpoint'"
        ).fetchone())
        self.conn.execute("INSERT INTO unrelated VALUES (1)")
        self.conn.execute("COMMIT")
        self.assertEqual(self.conn.execute("SELECT value FROM unrelated").fetchall(), [(1,)])

        self.initial()
        before = self.store.snapshot()
        self.conn.execute("BEGIN IMMEDIATE")
        with patch.object(self.store, "_read",
                          side_effect=[before, RuntimeError("injected post-update failure")]):
            with self.assertRaisesRegex(RuntimeError, "injected post-update failure"):
                self.store.observe(session_id=before.session_id, equity=D("98000"),
                                   receipt=B, expected_revision=before.revision,
                                   reconciled=True, marks_verified=True)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_marks").fetchone()[0], 0)
        self.conn.execute("INSERT INTO unrelated VALUES (2)")
        self.conn.execute("COMMIT")
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.conn.execute("SELECT value FROM unrelated ORDER BY value").fetchall(),
                         [(1,), (2,)])

    def test_recovered_equity_never_unlatches_historic_loss_after_restart(self):
        self.initial()
        breached = self.mark("98000.00", B)
        self.assertTrue(breached.halted)  # exact inclusive 2% boundary
        recovered = self.mark("99000.00", C)
        self.assertTrue(recovered.halted)  # later rebound cannot lift halt
        self.assertEqual(self.conn.execute(
            "SELECT reason,revision,mark_receipt FROM session_equity_halt_events"
        ).fetchall(), [("LOSS", breached.revision, B)])
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        self.assertEqual(self.store.snapshot(), recovered)
        # Corrupt only the current flag, leaving the historical mark and
        # irrevocable halt witness intact: a later $99k mark is NOT clean.
        self.conn.execute("UPDATE session_equity_checkpoint SET halted=0")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.mark("99500.00", "d" * 64)
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.rollover(previous_session="2026-11-02",
                                next_session="2026-11-03",
                                opening_equity=D("99000.00"), opening_receipt=A,
                                expected_revision=recovered.revision,
                                reconciled=True, zero_pending=True,
                                verified_calendar=True)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_marks").fetchone()[0], 2)

    def test_manual_halt_without_marks_has_immutable_durable_witness(self):
        initial = self.initial()
        manual = self.store.halt()
        self.assertTrue(manual.halted)
        self.assertEqual(manual.revision, initial.revision + 1)
        self.assertEqual(manual.observations, 0)
        self.assertEqual(self.store.halt(), manual)  # idempotent no new witness
        self.assertEqual(self.conn.execute(
            "SELECT reason,revision,mark_receipt FROM session_equity_halt_events"
        ).fetchall(), [("MANUAL", manual.revision, None)])
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        self.assertEqual(self.store.snapshot(), manual)
        for query in ("UPDATE session_equity_halt_events SET reason='LOSS'",
                      "DELETE FROM session_equity_halt_events"):
            with self.subTest(statement=query):
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            "IMMUTABLE_SESSION_HALT"):
                    self.conn.execute(query)
        self.conn.execute("UPDATE session_equity_checkpoint SET halted=0")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.halt()

    def test_halt_witness_rolls_back_with_outer_book_transaction(self):
        initial = self.initial()
        self.conn.execute("BEGIN IMMEDIATE")
        self.assertTrue(self.store.halt().halted)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_halt_events").fetchone()[0], 1)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.store.snapshot(), initial)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_halt_events").fetchone()[0], 0)
        self.conn.execute("BEGIN IMMEDIATE")
        with patch.object(self.store, "_read",
                          side_effect=[initial, RuntimeError("injected witness failure")]):
            with self.assertRaisesRegex(RuntimeError, "injected witness failure"):
                self.store.halt()
        # Operation-scoped SAVEPOINT must undo both the evidence insert and
        # the checkpoint flag even if the outer book transaction continues.
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_halt_events").fetchone()[0], 0)
        self.assertEqual(self.store.snapshot(), initial)
        self.conn.execute("COMMIT")
        self.assertEqual(self.store.snapshot(), initial)
        self.assertTrue(self.mark("98000.00", B).halted)
        self.conn.execute("UPDATE session_equity_checkpoint "
                          "SET session_id='2026-11-03', "
                          "opening_equity='98000.00',last_equity='98000.00', "
                          "last_mark_receipt=NULL,observations=0")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()

    def test_missing_halt_witness_is_corrupt_even_with_halted_checkpoint(self):
        self.initial()
        self.store.halt()
        self.conn.execute("DROP TABLE session_equity_halt_events")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()

    def test_new_portfolio_tighter_daily_loss_is_persisted_and_one_way(self):
        from alphora.paper_config import PaperRiskConfig

        policy = PaperRiskConfig(starting_equity_usd=D("120000"),
                                 daily_loss_fraction=D("0.01"))
        started = self.store.bootstrap("2026-11-02", D("120000"), A,
                                       proven_new_book=True, policy=policy)
        self.assertEqual(started.policy, policy)
        self.assertEqual(started.policy_receipt, policy.receipt())
        self.assertEqual(started.opening_equity, D("120000"))
        # 1% of the *new* starting equity, not 2% of the $100k default.
        self.assertFalse(self.mark("118800.01", B).halted)
        breached = self.mark("118800.00", C)
        self.assertTrue(breached.halted)
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        self.assertEqual(self.store.snapshot(), breached)
        self.assertTrue(self.mark("119999.99", "d" * 64).halted)
        self.assertEqual(self.store.snapshot().policy_receipt, policy.receipt())

    def test_tighter_daily_policy_survives_next_session_rollover(self):
        from alphora.paper_config import PaperRiskConfig

        policy = PaperRiskConfig(starting_equity_usd=D("120000"),
                                 daily_loss_fraction=D("0.01"))
        self.store.bootstrap("2026-11-02", D("120000"), A,
                             proven_new_book=True, policy=policy)
        self.mark("123000", B)
        before = self.store.snapshot()
        after = self.store.rollover(previous_session=before.session_id,
                                    next_session="2026-11-03",
                                    opening_equity=D("123000"), opening_receipt=C,
                                    expected_revision=before.revision,
                                    reconciled=True, zero_pending=True,
                                    verified_calendar=True)
        self.assertEqual(after.policy, policy)
        self.assertEqual(after.opening_equity, D("123000"))
        # Next-session 1% floor is $121,770, with a one-cent boundary.
        self.assertFalse(self.mark("121770.01", "d" * 64).halted)
        self.assertTrue(self.mark("121770.00", "e" * 64).halted)

    def test_policy_receipt_or_canonical_wire_tamper_fails_closed(self):
        self.initial()
        original_json, original_receipt = self.conn.execute(
            "SELECT policy_json,policy_receipt FROM session_equity_checkpoint"
        ).fetchone()
        # Ordinary writes may not silently relax or tighten a stored session
        # policy while preserving the same book/session revision.
        with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_SESSION_POLICY"):
            self.conn.execute("UPDATE session_equity_checkpoint SET policy_json=?",
                              (original_json.replace('"daily_loss_fraction":"0.02"',
                                                     '"daily_loss_fraction":"0.01"'),))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_SESSION_POLICY"):
            self.conn.execute("UPDATE session_equity_checkpoint SET policy_receipt=?",
                              ("f" * 64,))
        # Simulate a privileged direct-database tamper after dropping the SQL
        # trigger: the canonical wire and hash still fail closed on read.
        self.conn.execute("DROP TRIGGER session_equity_policy_no_update")
        self.conn.execute("UPDATE session_equity_checkpoint SET policy_json=?",
                          (original_json.replace('"daily_loss_fraction":"0.02"',
                                                 '"daily_loss_fraction":"0.01"'),))
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        self.conn.execute("UPDATE session_equity_checkpoint SET policy_json=?",
                          (original_json,))
        self.assertEqual(self.store.snapshot().policy_receipt, original_receipt)
        self.conn.execute("UPDATE session_equity_checkpoint SET policy_receipt=?",
                          ("f" * 64,))
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()

    def test_extreme_or_mismatched_new_portfolio_proposal_fails_closed(self):
        from alphora.paper_config import PaperRiskConfig

        policy = PaperRiskConfig(starting_equity_usd=D("120000"),
                                 daily_loss_fraction=D("0.01"))
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_STARTING_EQUITY"):
            self.store.bootstrap("2026-11-02", D("100000"), A,
                                 proven_new_book=True, policy=policy)
        # Older inert-config constructors can accept tiny fractions whose
        # expanded canonical text exceeds the bounded proposal wire domain.
        tiny = PaperRiskConfig(risk_per_trade_fraction=D("1e-63"))
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_POLICY"):
            self.store.bootstrap("2026-11-02", D("100000"), A,
                                 proven_new_book=True, policy=tiny)
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_POLICY"):
            self.store.bootstrap("2026-11-02", D("100000"), A,
                                 proven_new_book=True, policy=object())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_equity_checkpoint'"
        ).fetchone())

    def test_genesis_witness_rejects_session_or_opening_forgery(self):
        self.initial()
        first = self.mark("104000")
        for change in (
            "session_id='2026-11-03'",
            "opening_equity='101000.00'",
            "opening_receipt='" + C + "'",
        ):
            with self.subTest(change=change):
                self.conn.execute("UPDATE session_equity_checkpoint SET " + change)
                with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
                    self.store.snapshot()
                self.conn.execute(
                    "UPDATE session_equity_checkpoint SET "
                    "session_id=?,opening_equity=?,opening_receipt=?",
                    (first.session_id, str(first.opening_equity), first.opening_receipt))
        for statement in (
            "DELETE FROM session_equity_genesis",
            "UPDATE session_equity_genesis SET session_id='2026-11-03'",
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            "IMMUTABLE_SESSION_GENESIS"):
                    self.conn.execute(statement)
        self.assertEqual(self.store.snapshot(), first)

    def test_duplicate_rollover_requires_actual_matching_transition(self):
        # A genesis session is not a completed rollover, even if its opening
        # equity/receipt happen to match the alleged retry.
        original = self.initial()
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SESSION_REPLAY"):
            self.store.rollover(
                previous_session="2026-11-01",
                next_session=original.session_id,
                opening_equity=original.opening_equity,
                opening_receipt=original.opening_receipt,
                expected_revision=original.revision,
                reconciled=True, zero_pending=True, verified_calendar=True)
        self.assertEqual(self.store.snapshot(), original)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_rollovers").fetchone()[0], 0)

        self.mark("105000", B)
        completed = self.rollover("105000", C)
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        # A committed, exact request remains idempotent after restart, even
        # when replayed with the pre-rollover expected revision.
        repeat = self.store.rollover(
            previous_session="2026-11-02", next_session="2026-11-03",
            opening_equity=D("105000.00"), opening_receipt=C,
            expected_revision=completed.revision - 1,
            reconciled=True, zero_pending=True, verified_calendar=True)
        self.assertEqual(repeat, completed)
        # The same destination/equity/receipt must not falsely attest a
        # different predecessor or claim an entirely different transition.
        for wrong_previous in ("2026-11-01", "2026-11-03", "2026-11-04"):
            with self.subTest(previous=wrong_previous):
                with self.assertRaisesRegex(ValueError, "CONFLICTING_SESSION_REPLAY"):
                    self.store.rollover(
                        previous_session=wrong_previous,
                        next_session="2026-11-03",
                        opening_equity=D("105000"), opening_receipt=C,
                        expected_revision=completed.revision,
                        reconciled=True, zero_pending=True, verified_calendar=True)
        self.assertEqual(self.store.snapshot(), completed)
        self.assertEqual(self.conn.execute(
            "SELECT previous_session,next_session FROM session_equity_rollovers"
        ).fetchall(), [("2026-11-02", "2026-11-03")])

    def test_rollover_chain_survives_reopen_and_rejects_forged_opening(self):
        self.initial()
        self.mark("105000", B)
        first = self.rollover("105000", C)
        self.assertEqual(first.opening_equity, D("105000"))
        second_mark = self.mark("110000", "d" * 64)
        second = self.store.rollover(
            previous_session=second_mark.session_id,
            next_session="2026-11-04", opening_equity=D("110000"),
            opening_receipt="e" * 64, expected_revision=second_mark.revision,
            reconciled=True, zero_pending=True, verified_calendar=True)
        self.assertEqual(second.opening_equity, D("110000"))
        self.assertEqual(self.conn.execute(
            "SELECT previous_session,next_session,opening_equity "
            "FROM session_equity_rollovers ORDER BY ordinal").fetchall(), [
            ("2026-11-02", "2026-11-03", "105000"),
            ("2026-11-03", "2026-11-04", "110000")])
        self.conn.close()
        self.conn = sqlite3.connect(self.file, isolation_level=None)
        self.store = SessionEquityCheckpoint(self.conn)
        self.assertEqual(self.store.snapshot(), second)
        for change in ("opening_equity='99000'", "session_id='2026-11-05'"):
            with self.subTest(change=change):
                self.conn.execute("UPDATE session_equity_checkpoint SET " + change)
                with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
                    self.store.snapshot()
                self.conn.execute(
                    "UPDATE session_equity_checkpoint "
                    "SET opening_equity='110000',session_id='2026-11-04'")
        self.assertEqual(self.store.snapshot(), second)
        for statement in (
            "DELETE FROM session_equity_rollovers WHERE ordinal=1",
            "UPDATE session_equity_rollovers SET opening_equity='111000' "
            "WHERE ordinal=2",
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            "IMMUTABLE_SESSION_ROLLOVER"):
                    self.conn.execute(statement)

    def test_removed_or_tampered_prior_session_receipt_vetoes_replay(self):
        self.initial()
        self.mark("105000", B)
        closed = self.rollover("105000", C)
        self.assertEqual(self.store.snapshot(), closed)
        self.conn.execute("DROP TRIGGER session_equity_marks_no_delete")
        self.conn.execute("DELETE FROM session_equity_marks WHERE session_id='2026-11-02'")
        with self.assertRaisesRegex(ValueError, "SESSION_CHECKPOINT_CORRUPT"):
            self.store.snapshot()
        # Do not claim SHA-256 receipts are an authenticated protection
        # against an arbitrary local SQLite administrator.

    def test_rollover_witness_rolls_back_with_outer_transaction(self):
        self.initial()
        marked = self.mark("105000", B)
        self.conn.execute("BEGIN IMMEDIATE")
        changed = self.rollover("105000", C)
        self.assertEqual(changed.opening_equity, D("105000"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_rollovers").fetchone()[0], 1)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.store.snapshot(), marked)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_equity_rollovers").fetchone()[0], 0)

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
