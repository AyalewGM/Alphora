"""Synthetic-only atomic book valuation and persistent loss shutdown tests."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

from alphora.offline_portfolio_mark import SourceQuote
from alphora.offline_portfolio_observer import observe_offline_portfolio_mark
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount, INITIAL_EQUITY, RiskPolicy

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=ET)
SESSION = "2026-11-09"


def q(bid="80", ask="80.10", *, at=NOW, source=True, symbol="SPY"):
    return SourceQuote(symbol, D(bid), D(ask), at, source)


class OfflinePortfolioObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "isolated-paper.sqlite"
        self.book = OfflineReservations(self.path, session_id=SESSION)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def buy(self, *, fill=True):
        acct = PaperAccount(INITIAL_EQUITY, INITIAL_EQUITY,
                            INITIAL_EQUITY, D("0"), SESSION)
        self.book.reserve_buy(
            "synthetic-buy", acct, BuyProposal(
                "SPY", D("100"), D("100"), D("99")
            ), reconciled=True
        )
        if fill:
            self.book.record_hypothetical_fill(
                "synthetic-buy", "synthetic-fill", 100, D("100")
            )
        return self.book.snapshot()

    def observe(self, quotes=(), **kwargs):
        opts = {"as_of": NOW, "reconciled": True, "session_verified": True}
        opts.update(kwargs)
        return observe_offline_portfolio_mark(self.book, quotes, **opts)

    def test_cash_only_mark_valid_without_inventing_quotes(self):
        result = self.observe()
        self.assertTrue(result.valid)
        self.assertEqual(result.mark.account.equity, INITIAL_EQUITY)
        self.assertFalse(result.book.halted)
        self.assertEqual(result.book.positions, ())

    def test_exact_daily_loss_boundary_latches_on_real_ledger_holdings(self):
        purchased = self.buy()
        self.assertEqual(purchased.cash, D("90000"))
        self.assertEqual(purchased.positions, (("SPY", 100, D("10000")),))
        safe = self.observe((q(bid="80.01", ask="80.1"),))
        self.assertTrue(safe.valid)
        self.assertEqual(safe.mark.account.equity, D("98001"))
        self.assertFalse(safe.book.halted)
        breached = self.observe((q(),))
        self.assertTrue(breached.valid)
        self.assertEqual(breached.mark.account.equity, D("98000"))
        self.assertTrue(breached.mark.halt_required)
        self.assertTrue(breached.book.halted)
        self.assertEqual(self.book.snapshot().cash, purchased.cash)
        self.assertEqual(self.book.snapshot().positions, purchased.positions)

    def test_recovery_cannot_clear_persistent_halt_or_reserve(self):
        self.buy()
        self.assertTrue(self.observe((q(),)).book.halted)
        recovered = self.observe((q(bid="110", ask="110.1"),))
        self.assertTrue(recovered.valid)
        self.assertEqual(recovered.mark.account.equity, D("101000"))
        self.assertTrue(recovered.book.halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy(
                "after-loss", PaperAccount(D("101000"), INITIAL_EQUITY,
                    D("90000"), D("1000"), SESSION),
                BuyProposal("QQQ", D("1"), D("100"), D("99")),
                reconciled=True,
            )
        self.book.close()
        self.book = OfflineReservations(self.path, session_id=SESSION)
        self.assertTrue(self.book.snapshot().halted)
        self.assertTrue(self.observe((q(bid="111", ask="111.1"),)).book.halted)

    def test_clean_guard_does_not_turn_safe_mark_into_terminal_halt(self):
        self.buy()
        token = self.book.begin_reconciliation()
        self.assertTrue(self.book.snapshot().halted)
        safe = self.observe((q(bid="90", ask="90.1"),))
        self.assertTrue(safe.valid)
        self.assertFalse(safe.mark.halt_required)
        self.assertEqual(safe.mark.account.equity, D("99000"))
        self.assertEqual(self.book._meta("halted"), "0")
        self.assertFalse(self.book.finish_reconciliation(token, fatal=False))
        self.assertFalse(self.book.snapshot().halted)

    def test_breached_mark_during_clean_check_remains_terminally_halted(self):
        self.buy()
        token = self.book.begin_reconciliation()
        breached = self.observe((q(bid="79", ask="79.1"),))
        self.assertTrue(breached.valid)
        self.assertTrue(breached.mark.halt_required)
        self.assertEqual(breached.mark.account.equity, D("97900"))
        self.assertEqual(self.book._meta("halted"), "1")
        self.assertFalse(self.book.finish_reconciliation(token, fatal=False))
        self.assertTrue(self.book.snapshot().halted)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id=SESSION)
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(self.book._meta("halted"), "1")

    def test_tightened_book_policy_latches_quote_loss_and_survives_restart(self):
        tightened = RiskPolicy(daily_loss_fraction=D("0.01"))
        path = Path(self.temp.name) / "tightened.sqlite"
        book = OfflineReservations(path, session_id=SESSION, policy=tightened)
        try:
            acct = PaperAccount(INITIAL_EQUITY, INITIAL_EQUITY,
                                INITIAL_EQUITY, D("0"), SESSION)
            book.reserve_buy(
                "tightened-buy", acct,
                BuyProposal("SPY", D("100"), D("100"), D("99")),
                reconciled=True,
            )
            book.record_hypothetical_fill(
                "tightened-buy", "tightened-fill", 100, D("100")
            )
            def observe(bid):
                return observe_offline_portfolio_mark(
                    book, (q(bid=bid, ask="90.10"),),
                    as_of=NOW, reconciled=True, session_verified=True,
                )
            safe = observe("90.01")
            self.assertTrue(safe.valid)
            self.assertEqual(safe.mark.account.equity, D("99001"))
            self.assertFalse(safe.mark.halt_required)
            self.assertEqual(book._meta("halted"), "0")
            boundary = observe("90.00")
            self.assertTrue(boundary.valid)
            self.assertEqual(boundary.mark.account.equity, D("99000"))
            self.assertTrue(boundary.mark.halt_required)
            self.assertEqual(book._meta("halted"), "1")
            book.close()
            book = OfflineReservations(path, session_id=SESSION, policy=tightened)
            self.assertTrue(book.snapshot().halted)
            with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
                OfflineReservations(path, session_id=SESSION)
        finally:
            book.close()

    def test_pending_orders_block_quote_observation(self):
        before = self.buy(fill=False)
        denied = self.observe((q(),))
        self.assertFalse(denied.valid)
        self.assertEqual(denied.reason, "PENDING_ORDER_RECONCILIATION")
        self.assertIsNone(denied.mark)
        self.assertIsNone(denied.book)
        self.assertEqual(self.book.snapshot(), before)
        self.book.cancel_remaining("synthetic-buy")
        self.assertTrue(self.observe().valid)

    def test_untrusted_stale_inconsistent_quotes_fail_without_state(self):
        self.buy()
        before = self.book.snapshot()
        for quotes in (
            (),
            (q(source=False),),
            (q(at=NOW-timedelta(seconds=6)),),
            (q(at=NOW+timedelta(seconds=1)),),
            (q(bid="80", ask="82"),),
            (q(symbol="QQQ"),),
            (q(), q()),
            (q(bid="NaN"),),
        ):
            with self.subTest(quotes=quotes):
                denied = self.observe(quotes)
                self.assertFalse(denied.valid)
                self.assertIsNone(denied.book)
                self.assertIsNone(denied.mark)
                self.assertEqual(self.book.snapshot(), before)

    def test_nonpaper_bad_session_and_unreconciled_book_fail_closed(self):
        self.buy()
        initial = self.book.snapshot()
        for options in (
            {"mode": "live"},
            {"mode": "paper-connected"},
            {"session_verified": False},
            {"session_verified": 1},
            {"reconciled": False},
            {"reconciled": 1},
            {"as_of": datetime(2026, 11, 9, 10)},
            {"as_of": NOW + timedelta(days=1)},
        ):
            with self.subTest(options=options):
                result = self.observe((q(),), **options)
                self.assertFalse(result.valid)
                self.assertIsNone(result.book)
                self.assertEqual(self.book.snapshot(), initial)

    def test_sqlite_failure_rolls_back_halt_atomically(self):
        self.buy()
        before = self.book.snapshot()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER forced_stop_failure
                BEFORE UPDATE OF value ON book_meta
                WHEN NEW.key='halted' AND NEW.value='1'
                BEGIN SELECT RAISE(ABORT, 'forced halt failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe((q(),))
        self.assertEqual(self.book.snapshot(), before)
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER forced_stop_failure")
        self.assertTrue(self.observe((q(),)).book.halted)

    def test_second_connection_observes_committed_halt(self):
        self.buy()
        second = OfflineReservations(self.path, session_id=SESSION)
        try:
            self.assertFalse(second.snapshot().halted)
            result = self.observe((q(),))
            self.assertTrue(result.book.halted)
            self.assertTrue(second.snapshot().halted)
            self.assertEqual(second.snapshot().cash, D("90000"))
        finally:
            second.close()

    def test_no_external_order_or_feed_interfaces(self):
        import alphora.offline_portfolio_observer as module
        for attr in ("submit_order", "connect_alpaca", "download_quote",
                     "execute_trade", "start_scheduler", "broker_client"):
            self.assertFalse(hasattr(module, attr))
        self.assertFalse(hasattr(self.book, "submit_order"))


if __name__ == "__main__":
    unittest.main()
