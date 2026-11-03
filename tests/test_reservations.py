"""Offline SQLite cash-reservation, idempotency and synthetic-fill tests."""
import tempfile
import sqlite3
import threading
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from alphora.risk import BuyProposal, PaperAccount, INITIAL_EQUITY
from alphora.reservations import OfflineReservations


class OfflineReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.book = OfflineReservations(self.path, session_id="2026-11-02")

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def account(self, **changes):
        base = PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), "2026-11-02")
        return replace(base, **changes)

    def proposal(self, *, symbol="SPY", qty="100", price="50", stop="48", existing="0"):
        return BuyProposal(symbol, D(qty), D(price), D(stop), D(existing))

    def reserve(self, ident="a", proposal=None, **kw):
        return self.book.reserve_buy(ident, self.account(), proposal or self.proposal(),
                                     reconciled=True, **kw)

    def test_initial_zero_reservations_and_defaults(self):
        state = self.book.snapshot()
        self.assertEqual(state.cash, INITIAL_EQUITY)
        self.assertEqual(state.available_cash, INITIAL_EQUITY)
        self.assertEqual(state.pending, ())
        self.assertEqual(state.session_id, "2026-11-02")
        self.assertFalse(hasattr(self.book, "submit_order"))

    def test_pending_reserves_cash_and_exact_id_is_idempotent(self):
        first = self.reserve(fee_budget=D("4"))
        self.assertEqual(first.reserved_cash, D("5004"))
        self.assertEqual(first.available_cash, D("94996"))
        self.assertEqual(self.reserve(fee_budget=D("4")), first)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_INTENT_ID"):
            self.reserve(proposal=self.proposal(qty="90"), fee_budget=D("4"))

    def test_multiple_pending_intents_share_instrument_cap(self):
        self.reserve("a", self.proposal(qty="120"))
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_ALLOCATION_EXCEEDED"):
            self.reserve("b", self.proposal(qty="100"))
        self.reserve("b", self.proposal(qty="80"))
        self.assertEqual(self.book.snapshot().reserved_cash, D("10000"))

    def test_daily_loss_and_planned_trade_risk_veto(self):
        with self.assertRaisesRegex(ValueError, "TRADE_RISK_EXCEEDED"):
            self.reserve("a", self.proposal(qty="100", stop="44"))
        account = self.account(equity=D("98000"), cash=D("98000"), day_pnl=D("-2000"))
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.reserve_buy("b", account, self.proposal(), reconciled=True)
        with self.assertRaisesRegex(ValueError, "DAILY_LOSS_HALT"):
            self.book.reserve_buy("b", self.account(equity=D("98000"),
                day_pnl=D("-2000")), self.proposal(), reconciled=True)

    def test_daily_loss_veto_persists_one_way_halt_across_restart(self):
        self.reserve("pending", self.proposal(symbol="SPY", qty="20"))
        before = self.book.snapshot()
        self.assertFalse(before.halted)
        loss_account = self.account(equity=D("98000"), day_pnl=D("-2000"))
        with self.assertRaisesRegex(ValueError, "RISK_VETO:DAILY_LOSS_HALT"):
            self.book.reserve_buy("rejected", loss_account,
                                  self.proposal(symbol="QQQ"),
                                  reconciled=True)
        state = self.book.snapshot()
        self.assertTrue(state.halted)
        self.assertEqual(state.pending, before.pending)
        self.assertEqual(state.cash, before.cash)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)
        # A later caller assertion of recovered equity must not un-halt.
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.reserve("after-recovery", self.proposal(symbol="QQQ"))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_fill("pending", "fill-after-stop",
                                               1, D("50"))
        self.assertEqual(self.book.cancel_remaining("pending").reserved_cash, D(0))

    def test_idempotent_intent_retry_cannot_hide_new_daily_loss(self):
        original = self.reserve("same-id", self.proposal(qty="20"))
        self.assertFalse(original.halted)
        loss = self.account(equity=D("98000"), day_pnl=D("-2000"))
        with self.assertRaisesRegex(ValueError, "DAILY_LOSS_HALT"):
            self.book.reserve_buy("same-id", loss, self.proposal(qty="20"),
                                  reconciled=True)
        halted = self.book.snapshot()
        self.assertTrue(halted.halted)
        self.assertEqual(halted.pending, original.pending)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.reserve("other-id", self.proposal(symbol="QQQ"))

    def test_explicit_loss_observation_does_not_need_new_order(self):
        self.reserve("pending", self.proposal(qty="20"))
        observed = self.book.observe_daily_loss(
            self.account(equity=D("98000"), day_pnl=D("-2000")),
            reconciled=True)
        self.assertTrue(observed.halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_fill("pending", "f1", 1, D("50"))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)

    def test_observer_rejects_unreconciled_and_inconsistent_marks(self):
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNRECONCILED_ACCOUNT"):
            self.book.observe_daily_loss(self.account())
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.observe_daily_loss(self.account(cash=D("99999")),
                                         reconciled=True)
        with self.assertRaisesRegex(ValueError, "INCONSISTENT_ACCOUNT_STATE"):
            self.book.observe_daily_loss(self.account(day_pnl=D("-50")),
                                         reconciled=True)
        self.assertFalse(self.book.snapshot().halted)
        # A caller-asserted manual upstream stop must propagate one-way.
        self.assertTrue(self.book.observe_daily_loss(
            self.account(halted=True), reconciled=True).halted)

    def test_non_loss_risk_veto_does_not_trigger_permanent_halt(self):
        with self.assertRaisesRegex(ValueError, "TRADE_RISK_EXCEEDED"):
            self.reserve("bad", self.proposal(qty="100", stop="44"))
        self.assertFalse(self.book.snapshot().halted)
        self.reserve("still-valid", self.proposal(qty="20"))

    def test_session_open_equity_cannot_be_forged_to_raise_risk_limits(self):
        # The virtual book starts a single fixed session at $100k. A caller
        # cannot raise or lower the opening-loss baseline to avoid the 2% stop.
        for opening_equity in (D("98000"), D("105000")):
            with self.subTest(opening_equity=opening_equity):
                forged = self.account(session_open_equity=opening_equity,
                                      equity=opening_equity, day_pnl=D("0"))
                with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
                    self.book.reserve_buy("forged", forged, self.proposal(),
                                          reconciled=True)
        self.assertEqual(self.book.snapshot().pending, ())

    def test_reopen_rejects_corrupt_cash_and_keeps_state_unchanged(self):
        self.reserve("a")
        before = self.book.snapshot()
        self.book.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value='99999' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id="2026-11-02")
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value=? WHERE key='cash'",
                         (str(before.cash),))
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)

    def test_failed_sqlite_fill_rolls_back_atomically_across_restart(self):
        self.reserve("a", fee_budget=D("2"))
        before = self.book.snapshot()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TRIGGER injected_fill_failure
                BEFORE INSERT ON hypothetical_fills BEGIN
                    SELECT RAISE(ABORT, 'injected failure');
                END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.record_hypothetical_fill("a", "partial-1", 20, D("49"),
                                                fee=D("1"))
        self.assertEqual(self.book.snapshot(), before)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM hypothetical_fills").fetchone()[0], 0)
            conn.execute("DROP TRIGGER injected_fill_failure")
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)
        after = self.book.record_hypothetical_fill("a", "partial-1",
                                                   20, D("49"), fee=D("1"))
        self.assertEqual(after.cash, D("99019"))

    def test_cash_overcommit_across_pending_symbols(self):
        for i in range(10):
            self.reserve(f"i{i}", self.proposal(symbol=f"S{i}", qty="200"))
        self.assertEqual(self.book.snapshot().available_cash, D(0))
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_CASH"):
            self.reserve("extra", self.proposal(symbol="XYZ", qty="1"))

    def test_partial_fill_releases_price_improvement_and_keeps_remaining_locked(self):
        self.reserve("p", fee_budget=D("5"))
        state = self.book.record_hypothetical_fill("p", "f1", 40, D("49"), fee=D("2"))
        self.assertEqual(state.cash, D("98038"))
        self.assertEqual(state.reserved_cash, D("3003"))
        self.assertEqual(state.available_cash, D("95035"))
        self.assertEqual(state.positions, (("SPY", 40, D("1960")),))
        self.assertEqual(state.pending[0][2], 60)
        self.assertEqual(self.book.record_hypothetical_fill("p", "f1", 40, D("49"), fee=D("2")), state)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_FILL_ID"):
            self.book.record_hypothetical_fill("p", "f1", 40, D("49"), fee=D("3"))
        final = self.book.record_hypothetical_fill("p", "f2", 60, D("50"), fee=D("2"))
        self.assertEqual(final.cash, D("95036"))
        self.assertEqual(final.reserved_cash, D(0))
        self.assertEqual(final.positions, (("SPY", 100, D("4960")),))

    def test_no_overfill_gaps_or_unbudgeted_fees(self):
        self.reserve("a", fee_budget=D("1"))
        for qty, price, fee in ((101, D(50), D(0)), (1, D(51), D(0)),
                                (1, D(48), D(0)), (1, D(50), D(2))):
            with self.subTest(qty=qty, price=price, fee=fee):
                with self.assertRaisesRegex(ValueError, "FILL_EXCEEDS_LIMITS"):
                    self.book.record_hypothetical_fill("a", "new", qty, price, fee=fee)
        self.assertEqual(self.book.snapshot().cash, D("100000"))
        self.assertEqual(self.book.snapshot().reserved_cash, D("5001"))

    def test_cancel_releases_unfilled_reserve_but_retains_fills(self):
        self.reserve("a")
        self.book.record_hypothetical_fill("a", "f1", 20, D("50"))
        state = self.book.cancel_remaining("a")
        self.assertEqual(state.cash, D("99000"))
        self.assertEqual(state.reserved_cash, D(0))
        self.assertEqual(state.positions, (("SPY", 20, D("1000")),))
        self.assertEqual(self.book.cancel_remaining("a"), state)
        with self.assertRaisesRegex(ValueError, "NO_OPEN_INTENT"):
            self.book.record_hypothetical_fill("a", "f2", 1, D("50"))

    def test_restart_persists_reservations_fills_and_halt(self):
        self.reserve("a", fee_budget=D("1"))
        self.book.record_hypothetical_fill("a", "f1", 10, D("49"))
        old = self.book.snapshot()
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), old)
        self.assertEqual(self.book.record_hypothetical_fill("a", "f1", 10, D("49")), old)
        self.book.halt()
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.reserve("b", self.proposal(symbol="QQQ"))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_fill("a", "f2", 1, D("50"))
        self.assertEqual(self.book.cancel_remaining("a").reserved_cash, D(0))

    def test_reopen_mismatch_and_nondurable_paths_rejected(self):
        with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
            OfflineReservations(self.path, session_id="2026-11-03")
        for path in ("", " ", ":memory:", "file:paper?mode=memory", "file::memory:"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_BOOK"):
                    OfflineReservations(path, session_id="2026-11-02")

    def test_malformed_and_unreconciled_rejected(self):
        for ident in ("bad/id", "", "b"*129):
            with self.assertRaises(ValueError):
                self.reserve(ident)
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNRECONCILED_INTENT"):
            self.book.reserve_buy("a", self.account(), self.proposal(), reconciled=False)
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.reserve_buy("a", self.account(cash=D("99999")),
                                  self.proposal(), reconciled=True)
        for fill_qty in (0, 1.5, True, -1):
            with self.subTest(qty=fill_qty):
                with self.assertRaises(ValueError):
                    self.book.record_hypothetical_fill("a", "f", fill_qty, D("50"))

    def test_book_ledger_tampering_fails_closed(self):
        self.reserve("a")
        self.book.record_hypothetical_fill("a", "f1", 10, D("50"))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value='100000' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            self.book.snapshot()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value='99500' WHERE key='cash'")
            conn.execute("DELETE FROM hypothetical_fills")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            self.book.snapshot()

    def test_missing_meta_with_existing_history_does_not_reseed_book(self):
        self.reserve("a")
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM book_meta")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id="2026-11-02")

    def test_initial_capital_cannot_be_overridden(self):
        for amount in (D("100001"), D("99999")):
            with self.subTest(value=amount):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_BOOK"):
                    OfflineReservations(self.path, session_id="2026-11-02",
                                        starting_cash=amount)

    def test_cross_connection_transaction_prevents_double_reserve(self):
        attempts = []
        def attempt(label):
            book = OfflineReservations(self.path, session_id="2026-11-02")
            try:
                book.reserve_buy(label, PaperAccount(D("100000"), D("100000"),
                    D("100000"), D("0"), "2026-11-02"),
                    self.proposal(symbol="SPY", qty="200"), reconciled=True)
                attempts.append("ok")
            except ValueError as exc:
                attempts.append(str(exc))
            finally:
                book.close()
        threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(attempts.count("ok"), 1)
        self.assertEqual(self.book.snapshot().reserved_cash, D("10000"))


if __name__ == "__main__":
    unittest.main()
