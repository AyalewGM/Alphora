"""Offline, synthetic regression for the session-pinned daily-loss observer.

These tests do not authenticate price marks, prove cross-session rollover, or
authorize any unattended paper execution, broker access, or real orders (#42).
"""
from decimal import Decimal as D, localcontext
from pathlib import Path
import tempfile
import unittest

from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount, RiskPolicy, validate_buy


SESSION = "2026-11-02"
EQUITY = D("100000")


class PinnedDailyLossObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "book.sqlite"
        self.policy = RiskPolicy(daily_loss_fraction=D("0.01"))
        self.book = OfflineReservations(
            self.path, session_id=SESSION, policy=self.policy)

    def tearDown(self):
        self.book.close()
        self.directory.cleanup()

    def _hold(self, *, fee=D("0")):
        # A filled synthetic position makes cash <= marked equity, unlike a
        # fabricated cash-only $99k mark against an unchanged $100k cash book.
        proposal = BuyProposal("SPY", D("99"), D("100"), D("99"))
        opening = PaperAccount(EQUITY, EQUITY, EQUITY, D("0"), SESSION)
        self.book.reserve_buy(
            "existing", opening, proposal, fee_budget=fee, reconciled=True)
        self.book.record_hypothetical_fill(
            "existing", "synthetic-fill", 99, D("100"), fee=fee)
        self.assertEqual(self.book.snapshot().cash, D("90100") - fee)
        return proposal

    def _mark(self, equity):
        with localcontext() as ctx:
            ctx.prec = 80
            day_pnl = equity - EQUITY
        return PaperAccount(
            equity, EQUITY, self.book.snapshot().cash, day_pnl, SESSION)

    def test_tightened_exact_boundary_halts_without_a_new_buy_and_survives_restart(self):
        existing = self._hold()
        before = self.book.snapshot()
        mark = self._mark(D("99000"))
        verdict = validate_buy(
            mark, BuyProposal("QQQ", D("1"), D("50"), D("49")),
            expected_session_id=SESSION, policy=self.policy)
        self.assertFalse(verdict.permitted)
        self.assertEqual(verdict.reason, "DAILY_LOSS_HALT")

        # The risk observation itself, not a subsequent reservation, must
        # persist the stricter 1% one-way halt inside SQLite.
        observed = self.book.observe_daily_loss(mark, reconciled=True)
        self.assertTrue(observed.halted)
        self.assertEqual(observed.cash, before.cash)
        self.assertEqual(observed.positions, before.positions)
        self.assertEqual(observed.pending, before.pending)
        self.book.close()
        self.book = OfflineReservations(
            self.path, session_id=SESSION, policy=self.policy)
        self.assertTrue(self.book.snapshot().halted)
        self.assertTrue(
            self.book.observe_daily_loss(
                self._mark(D("99999")), reconciled=True).halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy(
                "existing", mark, existing, reconciled=True)
        with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
            OfflineReservations(self.path, session_id=SESSION)

    def test_one_cent_above_tightened_floor_is_not_a_loss_halt(self):
        self._hold()
        before = self.book.snapshot()
        just_above = self._mark(D("99000.01"))
        self.assertEqual(
            validate_buy(
                just_above, BuyProposal("QQQ", D("1"), D("50"), D("49")),
                expected_session_id=SESSION, policy=self.policy).reason,
            "PAPER_BUY_ALLOWED")
        self.assertFalse(
            self.book.observe_daily_loss(
                just_above, reconciled=True).halted)
        self.assertEqual(self.book.snapshot(), before)
        self.assertTrue(
            self.book.observe_daily_loss(
                self._mark(D("99000.00")), reconciled=True).halted)

    def test_fee_inclusive_mark_and_deeper_loss_latch_without_buy(self):
        self._hold(fee=D("0.01"))
        self.assertEqual(self.book.snapshot().cash, D("90099.99"))
        # These are caller-asserted marks; the 1-cent fee was actually
        # recorded as a synthetic fill debit, not rounded out of cash.
        self.assertFalse(
            self.book.observe_daily_loss(
                self._mark(D("99000.01")), reconciled=True).halted)
        self.assertTrue(
            self.book.observe_daily_loss(
                self._mark(D("98500")), reconciled=True).halted)
        self.book.close()
        self.book = OfflineReservations(
            self.path, session_id=SESSION, policy=self.policy)
        self.assertTrue(self.book.snapshot().halted)

    def test_one_cent_fill_fee_tips_exact_halt_boundary(self):
        # At the approved 10% symbol ceiling, a one-share $9,999.99
        # synthetic fill plus a one-cent fee spends exactly $10,000.
        # A caller-asserted $9,000 position mark is a $999.99 loss
        # without the fee, but an exact $1,000 loss with the booked fee.
        proposal = BuyProposal(
            "SPY", D("1"), D("9999.99"), D("9999.98"))
        opening = PaperAccount(EQUITY, EQUITY, EQUITY, D("0"), SESSION)
        self.book.reserve_buy(
            "near-cap", opening, proposal,
            fee_budget=D("0.01"), reconciled=True)
        filled = self.book.record_hypothetical_fill(
            "near-cap", "one-share-fill", 1, D("9999.99"),
            fee=D("0.01"))
        self.assertEqual(filled.cash, D("90000"))
        self.assertEqual(EQUITY - D("9999.99") + D("9000"), D("99000.01"))
        self.assertEqual(filled.positions, (("SPY", 1, D("10000.00")),))
        after_fee_mark = self._mark(filled.cash + D("9000"))
        self.assertEqual(after_fee_mark.equity, D("99000"))
        self.assertTrue(self.book.observe_daily_loss(
            after_fee_mark, reconciled=True).halted)
        self.book.close()
        self.book = OfflineReservations(
            self.path, session_id=SESSION, policy=self.policy)
        self.assertTrue(self.book.snapshot().halted)

    def test_unverified_or_wrong_session_marks_do_not_change_book(self):
        self._hold()
        before = self.book.snapshot()
        mark = self._mark(D("99000"))
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNRECONCILED_ACCOUNT"):
            self.book.observe_daily_loss(mark, reconciled=False)
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNRECONCILED_ACCOUNT"):
            self.book.observe_daily_loss(None, reconciled=True)
        wrong_session = PaperAccount(
            D("99000"), D("100000"), before.cash, D("-1000"),
            "2026-11-03")
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.observe_daily_loss(wrong_session, reconciled=True)
        wrong_open = PaperAccount(
            D("99000"), D("105000"), before.cash, D("-16000"),
            SESSION)
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.observe_daily_loss(wrong_open, reconciled=True)
        self.assertEqual(self.book.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
