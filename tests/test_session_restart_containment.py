"""Issue #42: restart must not bypass the bounded single-session containment.

Synthetic-only negative tests. These DO NOT implement session rollover, attest
market marks, or authorize unattended paper execution or broker connectivity.
"""
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from alphora.broker.paper import PaperBrokerAdapter
from alphora.orders import OrderLedger
from alphora.research.backtest import Bar
from alphora.reservations import OfflineReservations
from alphora.runner import PaperSessionRunner, SessionDay, SessionSignal


SESSION_ID = "2026-11-02"


def asserted_day(day_number, *, enter):
    trading_day = date(2026, 11, day_number)
    mark = D("50")
    bar = Bar(trading_day, mark, mark, mark, mark, 1000)
    signals = {"SPY": SessionSignal("WATCHLIST", "VERIFIED")} if enter else {}
    return SessionDay(
        trading_day.isoformat(),
        datetime(2026, 11, day_number, 21, tzinfo=timezone.utc),
        {"SPY": bar},
        signals,
    )


class RestartContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book_path = root / "book.sqlite"
        self.ledger_path = root / "ledger.sqlite"
        self.venue = PaperBrokerAdapter()
        self._open()

    def _open(self):
        self.book = OfflineReservations(self.book_path, session_id=SESSION_ID)
        self.ledger = OrderLedger(self.ledger_path, session_id=SESSION_ID)
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger,
            adapter=self.venue, take_profit_pct=D("50"),
        )

    def _restart(self):
        self.book.close()
        self.ledger.close()
        self._open()

    def tearDown(self):
        self.book.close()
        self.ledger.close()
        self.tmp.cleanup()

    def test_cross_session_still_rejected_after_reopening_sqlite(self):
        first = self.runner.run((asserted_day(2, enter=True),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        self._restart()
        before = (self.book.snapshot(), self.ledger.orders(),
                  self.venue.positions(), self.venue.open_orders())
        # An in-memory last-day field resets on restart. Persisted session
        # identity must remain authoritative and reject another date.
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            self.runner.run((asserted_day(3, enter=True),))
        self.assertEqual(
            (self.book.snapshot(), self.ledger.orders(),
             self.venue.positions(), self.venue.open_orders()), before,
        )

    def test_new_session_fails_closed_after_cold_venue_restart(self):
        # Unlike the warm restart test, this loses the entire in-memory
        # simulated venue. It verifies containment only, NOT order recovery.
        first = self.runner.run((asserted_day(2, enter=True),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        persisted = (self.book.snapshot(), self.ledger.orders())
        self.book.close()
        self.ledger.close()
        self.venue = PaperBrokerAdapter()  # no positions/orders recovered
        self._open()
        self.assertEqual(
            (self.book.snapshot(), self.ledger.orders()), persisted,
        )
        self.assertEqual(self.venue.positions(), ())
        self.assertEqual(self.venue.open_orders(), ())
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            self.runner.run((asserted_day(3, enter=True),))
        self.assertEqual(
            (self.book.snapshot(), self.ledger.orders()), persisted,
        )
        self.assertEqual(self.venue.positions(), ())
        self.assertEqual(self.venue.open_orders(), ())
        self.assertIsNone(self.runner._last_day)

    def test_same_session_cold_restart_surfaces_fatal_drift_without_new_orders(self):
        # A complete process restart loses the in-memory paper venue.
        # Replaying the *same* date must not fabricate a clean venue/account
        # state or dispatch more orders based on the persisted book alone.
        # This is a bounded regression only: the runner currently reports
        # FATAL rather than refusing this replay before the venue is marked.
        first = self.runner.run((asserted_day(2, enter=True),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        persisted = (self.book.snapshot(), self.ledger.orders())
        self.book.close()
        self.ledger.close()
        self.venue = PaperBrokerAdapter()  # no recovered fills/positions
        self._open()
        replay = self.runner.run((asserted_day(2, enter=True),))
        self.assertEqual(replay.days[0].recon_status, "FATAL")
        # #58 has landed on main: FATAL must persist a one-way stop.
        self.assertTrue(replay.halted)
        self.assertTrue(self.book.snapshot().halted)
        self.assertFalse(
            any(a.startswith(("ENTRY:", "EXIT:")) for a in replay.days[0].actions),
            replay.days[0].actions,
        )
        before_book, before_orders = persisted
        after_book = self.book.snapshot()
        # A FATAL reconciliation must durably arm the one-way safety halt.
        # That is the ONLY permitted book mutation:
        # the replay must not change cash, positions, reserved funds, pending
        # intents, or the ledger to hide the lost venue state.
        self.assertEqual(
            replace(after_book, halted=before_book.halted), before_book,
        )
        self.assertEqual(self.ledger.orders(), before_orders)
        self.assertEqual(replay.halted, after_book.halted)
        self.assertEqual(self.venue.positions(), ())
        self.assertEqual(self.venue.open_orders(), ())
        # Verify the durable halt is not merely an in-memory flag that
        # disappears on SQLite reopening.
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(self.ledger.orders(), before_orders)
        # FATAL -> halted=True is now required here as well as in #58 tests.
        # This is still single-session containment, not session rollover.

    def test_one_way_manual_halt_survives_reopening_sqlite(self):
        self.runner.run((asserted_day(2, enter=True),))
        self.book.halt()
        before_orders = self.ledger.orders()
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        report = self.runner.run((asserted_day(2, enter=False),))
        self.assertTrue(report.halted)
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(self.ledger.orders(), before_orders)
        # A restarted process cannot use a different session id as an
        # implicit way to clear the persisted halt.
        self.book.close()
        self.ledger.close()
        with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
            OfflineReservations(self.book_path, session_id="2026-11-03")
        self._open()
        self.assertTrue(self.book.snapshot().halted)


if __name__ == "__main__":
    unittest.main()
