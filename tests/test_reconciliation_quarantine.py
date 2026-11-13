"""Synthetic crash/quarantine regressions; not vendor or rollover acceptance.

The provisional one-session guard is not a substitute for durable exchange-
session opening equity, actual marks, or independent multi-session QA (#42).
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alphora.broker.base import DispatchRequest
from alphora.broker.paper import PaperBrokerAdapter
from alphora.orders import OrderLedger
from alphora.research.backtest import Bar
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount
from alphora.runner import PaperSessionRunner, SessionDay, SessionSignal


SESSION = "2026-11-02"
WATCH = SessionSignal("WATCHLIST", "VERIFIED")


def session_day(day=2, *, enter=True):
    value = D("50")
    bar = Bar(date(2026, 11, day), value, value, value, value, 1000)
    return SessionDay(
        date(2026, 11, day).isoformat(),
        datetime(2026, 11, day, 21, tzinfo=timezone.utc),
        {"SPY": bar},
        {"SPY": WATCH} if enter else {},
    )


class DurableReconciliationQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.book_path = root / "book.sqlite"
        self.ledger_path = root / "ledger.sqlite"
        self.venue = PaperBrokerAdapter()
        self._open()

    def _open(self):
        self.book = OfflineReservations(self.book_path, session_id=SESSION)
        self.ledger = OrderLedger(self.ledger_path, session_id=SESSION)
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=self.venue,
            take_profit_pct=D("50"))

    def _restart(self, *, cold_venue=False):
        self.book.close()
        self.ledger.close()
        if cold_venue:
            self.venue = PaperBrokerAdapter()
        self._open()

    def tearDown(self):
        if self.book is not None:
            self.book.close()
        self.ledger.close()
        self.directory.cleanup()

    def _account(self):
        return PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), SESSION)

    def _proposal(self):
        return BuyProposal("SPY", D("20"), D("50"), D("48"))

    def test_clean_transaction_stays_dispatchable(self):
        token = self.book.begin_reconciliation()
        self.assertEqual(len(token), 32)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("no-buy-during-check", self._account(),
                                  self._proposal(), reconciled=True)
        self.assertFalse(self.book.finish_reconciliation(token, fatal=False))
        self.assertFalse(self.book.reconciliation_guarded())
        self.assertFalse(self.book.snapshot().halted)
        approved = self.book.reserve_buy("after-clean", self._account(),
                                         self._proposal(), reconciled=True)
        self.assertEqual(len(approved.pending), 1)

    def test_interrupted_check_stays_guarded_across_restart(self):
        token = self.book.begin_reconciliation()
        self.assertIsNotNone(token)
        original = self.book.snapshot()
        self._restart()
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(self.book.snapshot(), original)
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("blocked-replay", self._account(),
                                  self._proposal(), reconciled=True)
        self.assertTrue(self.book.snapshot().halted)

    def test_fatal_guard_survives_clean_rechecks_and_allows_acked_fill_settlement(self):
        self.book.reserve_buy("accepted", self._account(),
                              self._proposal(), reconciled=True)
        token = self.book.begin_reconciliation()
        self.assertTrue(self.book.finish_reconciliation(token, fatal=True))
        self.assertTrue(self.book.snapshot().halted)
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        # The quarantine vetoes NEW BUYs, but does not prematurely write
        # the terminal book halt and thereby destroy accepted fill recovery.
        landed = self.book.record_hypothetical_fill(
            "accepted", "accepted-fill", 20, D("50"))
        self.assertEqual(landed.positions, (("SPY", 20, D("1000")),))
        self.assertEqual(landed.cash, D("99000"))
        self.assertTrue(landed.halted)
        self.book.halt()
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("no-new-entry", self._account(),
                                  self._proposal(), reconciled=True)

    def test_runner_fatal_crash_before_verdict_remains_one_way_guarded(self):
        self.venue.dispatch(
            DispatchRequest("QQQ", "BUY", 1, D("10")),
            client_order_id="z" * 64)
        # Force an interruption AFTER reconcile() observed FATAL, BEFORE
        # its verdict is committed. The preceding CHECKING marker must be
        # durable, and a restarted process cannot reinterpret it as CLEAN.
        def interrupted(*_args, **_kwargs):
            raise RuntimeError("INJECTED_POST_RECONCILIATION_CRASH")

        self.book.finish_reconciliation = interrupted
        with self.assertRaisesRegex(RuntimeError, "INJECTED_POST"):
            self.runner.run((session_day(),))
        self.assertTrue(self.book.reconciliation_guarded())
        before_orders = self.ledger.orders()
        self._restart()  # preserve rogue in-memory venue for evidence
        self.assertTrue(self.book.snapshot().halted)
        result = self.runner.run((session_day(),))
        self.assertEqual(result.days[0].recon_status, "FATAL")
        self.assertTrue(result.halted)
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(self.ledger.orders(), before_orders)
        self.assertFalse(
            any(a.startswith("ENTRY:") for a in result.days[0].actions))
        self._restart()
        self.assertTrue(self.book.snapshot().halted)

    def test_cold_venue_replay_latches_fatal_without_rewriting_ledger(self):
        first = self.runner.run((session_day(),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        old_book, old_orders = self.book.snapshot(), self.ledger.orders()
        self._restart(cold_venue=True)
        replay = self.runner.run((session_day(),))
        self.assertEqual(replay.days[0].recon_status, "FATAL")
        self.assertTrue(replay.halted)
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(
            replace(self.book.snapshot(), halted=old_book.halted), old_book)
        self.assertEqual(self.ledger.orders(), old_orders)
        self.assertFalse(
            any(a.startswith(("ENTRY:", "EXIT:"))
                for a in replay.days[0].actions))
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            self.runner.run((session_day(3),))

    def test_clean_runner_releases_guard_after_every_check(self):
        report = self.runner.run((session_day(),))
        self.assertEqual(report.days[0].recon_status, "CLEAN")
        self.assertFalse(report.halted)
        self.assertFalse(self.book.reconciliation_guarded())
        self._restart()
        self.assertFalse(self.book.reconciliation_guarded())

    def test_missing_versioned_guard_does_not_silently_reseed_clear(self):
        self.book.close()
        with sqlite3.connect(self.book_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            db.execute("DELETE FROM reconciliation_guard")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.book_path, session_id=SESSION)
        self.book = None

    def test_concurrent_check_cannot_clear_other_check_token(self):
        first = self.book.begin_reconciliation()
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        with self.assertRaisesRegex(ValueError, "RECONCILIATION_GUARD_CONFLICT"):
            self.book.finish_reconciliation("a" * 32, fatal=False)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertFalse(self.book.finish_reconciliation(first, fatal=False))
        self.assertFalse(self.book.reconciliation_guarded())


if __name__ == "__main__":
    unittest.main()
