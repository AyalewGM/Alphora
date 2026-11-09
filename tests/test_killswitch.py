"""Kill-switch halt file, book halt and venue cancel-all behaviour."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from alphora.broker.manager import PaperOrderManager
from alphora.broker.paper import PaperBrokerAdapter
from alphora.decision import EntryPlan
from alphora.killswitch import KillSwitch
from alphora.orders import OrderLedger
from alphora.reconcile import reconcile
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
KEY = "a" * 64


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book = OfflineReservations(root / "book.sqlite",
                                        session_id="2026-11-02")
        self.ledger = OrderLedger(root / "orders.sqlite",
                                  session_id="2026-11-02")
        self.venue = PaperBrokerAdapter()
        self.halt_file = root / "HALT"
        self.switch = KillSwitch(book=self.book, adapter=self.venue,
                                 halt_file=self.halt_file)
        self.mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                     adapter=self.venue, killswitch=self.switch)

    def tearDown(self):
        self.book.close()
        self.ledger.close()
        self.tmp.cleanup()

    def account(self):
        return PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), "2026-11-02")

    def recon(self):
        return reconcile(self.book, self.ledger, self.venue, at=at())

    def enter(self, key=KEY):
        plan = EntryPlan(True, "PAPER_BUY_ALLOWED",
                         BuyProposal("SPY", D("100"), D("50"), D("48")), key)
        return self.mgr.execute_entry(plan, self.account(), recon=self.recon(),
                                      now=at(), reconciled=True)

    def test_config_validation_fails_closed(self):
        for bad in ("", "   ", "no/such/dir/HALT",
                    Path(self.tmp.name)):
            with self.subTest(path=bad):
                with self.assertRaisesRegex(ValueError, "INVALID_KILL_SWITCH_CONFIG"):
                    KillSwitch(book=self.book, adapter=self.venue,
                               halt_file=bad)

    def test_halt_file_engages_and_blocks_dispatch(self):
        self.assertFalse(self.switch.is_halted())
        self.halt_file.write_text('{"reason":"operator"}')
        self.assertTrue(self.switch.file_engaged())
        self.assertTrue(self.switch.is_halted())
        with self.assertRaisesRegex(ValueError, "KILL_SWITCH_ENGAGED:HALT_FILE"):
            self.switch.assert_clear()
        with self.assertRaisesRegex(ValueError, "KILL_SWITCH_ENGAGED"):
            self.enter()

    def test_book_halt_alone_blocks_via_kill_switch(self):
        self.book.halt()
        self.assertTrue(self.switch.is_halted())
        with self.assertRaisesRegex(ValueError, "KILL_SWITCH_ENGAGED:BOOK_HALTED"):
            self.switch.assert_clear()

    def test_kill_halts_book_and_cancels_open_orders(self):
        self.enter()
        self.assertEqual(len(self.venue.open_orders()), 1)
        result = self.switch.kill("daily loss breach", now=at(1))
        self.assertTrue(result.halted)
        self.assertEqual(result.cancelled, ("paper-00000001",))
        self.assertEqual(result.errors, ())
        self.assertTrue(self.book.snapshot().halted)
        self.assertTrue(self.halt_file.exists())
        self.assertEqual(self.venue.order_status("paper-00000001").status,
                         "CANCELLED")
        # The venue cancel lands in the ledger on the next sync.
        self.mgr.sync(now=at(2))
        self.assertEqual(self.ledger.get(KEY).status, "CANCELLED")
        self.assertEqual(self.book.snapshot().reserved_cash, D(0))
        # New dispatch is dead at both layers afterwards.
        with self.assertRaisesRegex(ValueError, "KILL_SWITCH_ENGAGED"):
            self.enter("b" * 64)

    def test_kill_collects_cancel_errors_and_still_halts(self):
        class FlakyVenue(PaperBrokerAdapter):
            def cancel(self, broker_order_id):
                if broker_order_id == "paper-00000002":
                    raise RuntimeError("venue unreachable")
                return super().cancel(broker_order_id)
        venue = FlakyVenue()
        switch = KillSwitch(book=self.book, adapter=venue,
                            halt_file=self.halt_file)
        mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                adapter=venue, killswitch=switch)
        plan = EntryPlan(True, "PAPER_BUY_ALLOWED",
                         BuyProposal("SPY", D("10"), D("50"), D("48")), KEY)
        mgr.execute_entry(plan, self.account(),
                          recon=reconcile(self.book, self.ledger, venue,
                                          at=at()), now=at(), reconciled=True)
        plan2 = EntryPlan(True, "PAPER_BUY_ALLOWED",
                          BuyProposal("QQQ", D("10"), D("50"), D("48")),
                          "c" * 64)
        mgr.execute_entry(plan2, self.account(),
                          recon=reconcile(self.book, self.ledger, venue,
                                          at=at()), now=at(), reconciled=True)
        result = switch.kill("operator", now=at(1))
        self.assertEqual(result.cancelled, ("paper-00000001",))
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(self.halt_file.exists())

    def test_kill_preserves_first_reason_and_is_idempotent(self):
        first = self.switch.kill("first reason", now=at(1))
        second = self.switch.kill("second reason", now=at(2))
        self.assertTrue(second.halted)
        content = self.halt_file.read_text()
        self.assertIn("first reason", content)
        self.assertNotIn("second reason", content)

    def test_resume_removes_file_but_book_halt_persists(self):
        self.halt_file.write_text('{"reason":"operator"}')
        result = self.switch.resume()
        self.assertFalse(self.halt_file.exists())
        self.assertFalse(result.halted)
        self.switch.assert_clear()
        self.switch.kill("loss", now=at())
        result = self.switch.resume()
        self.assertTrue(result.halted)  # book halt is one-way for the session
        self.assertFalse(self.halt_file.exists())
        with self.assertRaisesRegex(ValueError, "BOOK_HALTED"):
            self.switch.assert_clear()

    def test_kill_validation_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "INVALID_REASON"):
            self.switch.kill("", now=at())
        with self.assertRaisesRegex(ValueError, "INVALID_TIMESTAMP"):
            self.switch.kill("reason", now=datetime(2026, 11, 2))


if __name__ == "__main__":
    unittest.main()
