"""Reconciliation severity model: CLEAN/WARN/FATAL diff classification."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from alphora.broker.base import DispatchRequest
from alphora.broker.manager import PaperOrderManager
from alphora.broker.paper import PaperBrokerAdapter
from alphora.decision import EntryPlan
from alphora.orders import OrderLedger
from alphora.reconcile import reconcile, Reconciliation
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
KEY = "a" * 64


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


def kinds(recon):
    return {d.kind for d in recon.diffs}


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book = OfflineReservations(root / "book.sqlite",
                                        session_id="2026-11-02")
        self.ledger = OrderLedger(root / "orders.sqlite",
                                  session_id="2026-11-02")
        self.venue = PaperBrokerAdapter()
        self.mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                     adapter=self.venue)

    def tearDown(self):
        self.book.close()
        self.ledger.close()
        self.tmp.cleanup()

    def account(self):
        return PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), "2026-11-02")

    def recon(self, **kw):
        return reconcile(self.book, self.ledger, self.venue, at=at(), **kw)

    def enter(self, key=KEY, qty="100", price="50", stop="48", symbol="SPY"):
        plan = EntryPlan(True, "PAPER_BUY_ALLOWED",
                         BuyProposal(symbol, D(qty), D(price), D(stop)), key)
        return self.mgr.execute_entry(plan, self.account(),
                                      recon=self.recon(), now=at(),
                                      reconciled=True)

    def test_clean_empty_state(self):
        recon = self.recon()
        self.assertEqual(recon.status, "CLEAN")
        self.assertEqual(recon.diffs, ())
        self.assertEqual(recon.session_id, "2026-11-02")

    def test_clean_after_settled_entry(self):
        self.enter()
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        recon = self.recon()
        self.assertEqual(recon.status, "CLEAN")
        self.assertEqual(recon.book_cash, recon.venue_cash)

    def test_acked_open_order_is_clean(self):
        self.enter()
        self.assertEqual(self.recon().status, "CLEAN")

    def test_unsettled_venue_fill_warns_then_heals(self):
        self.enter()
        self.venue.advance({"SPY": D("49")}, at=at(1))  # venue fills, unsynced
        recon = self.recon()
        self.assertEqual(recon.status, "WARN")
        self.assertIn("UNSETTLED_FILLS", kinds(recon))
        self.mgr.sync(now=at(2))
        self.assertEqual(self.recon().status, "CLEAN")

    def test_created_order_warns_undelivered(self):
        self.book.reserve_buy(KEY, self.account(),
                              BuyProposal("SPY", D("10"), D("50"), D("48")),
                              reconciled=True)
        self.ledger.create(KEY, symbol="SPY", side="BUY", qty=10,
                           limit_price=D("50"), stop_price=D("48"), now=at())
        recon = self.recon()
        self.assertEqual(recon.status, "WARN")
        self.assertIn("UNDELIVERED_INTENT", kinds(recon))
        self.mgr.cancel(KEY, now=at(1))
        self.assertEqual(self.recon().status, "CLEAN")

    def test_sent_order_warns_lost_ack(self):
        self.book.reserve_buy(KEY, self.account(),
                              BuyProposal("SPY", D("10"), D("50"), D("48")),
                              reconciled=True)
        self.ledger.create(KEY, symbol="SPY", side="BUY", qty=10,
                           limit_price=D("50"), stop_price=D("48"), now=at())
        self.ledger.mark_sent(KEY, now=at(1))
        recon = self.recon()
        self.assertEqual(recon.status, "WARN")
        self.assertIn("LOST_ACK", kinds(recon))
        self.mgr.sync(now=at(2))  # absent at venue -> confirmed absent
        self.assertEqual(self.recon().status, "CLEAN")

    def test_cancel_pending_warns_in_flight(self):
        self.enter()
        self.ledger.request_cancel(KEY, now=at(1))  # venue not yet cancelled
        recon = self.recon()
        self.assertEqual(recon.status, "WARN")
        self.assertIn("CANCEL_IN_FLIGHT", kinds(recon))
        self.venue.cancel("paper-00000001")
        recon = self.recon()
        self.assertEqual(recon.status, "WARN")  # venue dead, ledger pending
        self.mgr.sync(now=at(2))
        self.assertEqual(self.recon().status, "CLEAN")

    def test_unknown_venue_order_is_fatal(self):
        self.venue.dispatch(DispatchRequest("QQQ", "BUY", 5, D("10")),
                            client_order_id="z" * 64)
        recon = self.recon()
        self.assertEqual(recon.status, "FATAL")
        self.assertIn("VENUE_ORDER_UNKNOWN", kinds(recon))
        self.assertIn("RESERVED_MISMATCH", kinds(recon))

    def test_venue_position_divergence_is_fatal(self):
        self.enter()
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        # Venue gains a position the book never recorded.
        self.venue.dispatch(DispatchRequest("QQQ", "BUY", 10, D("10")),
                            client_order_id="z" * 64)
        self.venue.advance({"QQQ": D("10")}, at=at(2))
        recon = self.recon()
        self.assertEqual(recon.status, "FATAL")
        self.assertIn("POSITION_MISMATCH", kinds(recon))
        self.assertIn("CASH_MISMATCH", kinds(recon))

    def test_ledger_terminal_but_venue_still_open_is_fatal(self):
        self.enter()
        # Ledger reaches CANCELLED while the venue keeps the order working.
        self.ledger.request_cancel(KEY, now=at(1))
        self.ledger.mark_cancelled(KEY, now=at(2))
        recon = self.recon()
        self.assertEqual(recon.status, "FATAL")
        self.assertIn("STALE_VENUE_ORDER", kinds(recon))

    def test_tampered_book_cash_fails_closed_at_snapshot(self):
        self.enter()
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        # The book's own cash/fill invariant rejects tampering before
        # reconcile() could ever produce a misleading certificate.
        with sqlite3.connect(Path(self.tmp.name) / "book.sqlite") as conn:
            conn.execute("UPDATE book_meta SET value='1' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            self.recon()

    def test_session_mismatch_and_input_validation(self):
        with self.assertRaisesRegex(ValueError, "INVALID_RECONCILE_INPUT"):
            reconcile(self.book, self.ledger, self.venue,
                      at=datetime(2026, 11, 2))
        with self.assertRaisesRegex(ValueError, "INVALID_RECONCILE_INPUT"):
            reconcile(self.book, self.ledger, self.venue, at=at(),
                      cash_epsilon=D("-1"))
        other = OrderLedger(Path(self.tmp.name) / "o2.sqlite",
                            session_id="2026-11-03")
        try:
            with self.assertRaisesRegex(ValueError, "SESSION_MISMATCH"):
                reconcile(self.book, other, self.venue, at=at())
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
