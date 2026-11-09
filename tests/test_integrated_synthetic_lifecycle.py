"""Single-SQLite synthetic BUY/SELL lifecycle acceptance tests.

These tests do not create market orders, model authenticated fills, or
connect to any broker.
"""
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount


SESSION = "2026-11-02"


def account(cash=D("100000")):
    return PaperAccount(D("100000"), D("100000"), cash, D("0"), SESSION)


def proposal(qty=D("20")):
    return BuyProposal("SPY", qty, D("50"), D("48"))


class IntegratedSyntheticLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.sqlite"
        self.book = OfflineReservations(self.path, session_id=SESSION)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def buy(self, *, qty=20, filled=20):
        self.book.reserve_buy("buy-a", account(), proposal(D(qty)),
                              reconciled=True)
        return self.book.record_hypothetical_fill(
            "buy-a", "synthetic-buy-a", filled, D("49"))

    def test_single_database_buy_partial_sell_final_sell_and_restart(self):
        bought = self.buy()
        self.assertEqual(bought.cash, D("99020"))
        self.assertEqual(bought.positions, (("SPY", 20, D("980")),))
        partial = self.book.record_hypothetical_sell(
            "sell-a", "SPY", 10, D("52"), fee=D("2"))
        self.assertEqual(partial.cash, D("99538"))
        self.assertEqual(partial.positions, (("SPY", 10, D("490")),))
        self.assertEqual(partial.realized_pnl, D("28"))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id=SESSION)
        self.assertEqual(self.book.snapshot(), partial)
        self.assertEqual(self.book.record_hypothetical_sell(
            "sell-a", "SPY", 10, D("52"), fee=D("2")), partial)
        final = self.book.record_hypothetical_sell(
            "sell-b", "SPY", 10, D("45"), fee=D("1"))
        self.assertEqual(final.cash, D("99987"))
        self.assertEqual(final.positions, ())
        self.assertEqual(final.realized_pnl, D("-13"))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id=SESSION)
        self.assertEqual(self.book.snapshot(), final)

    def test_sell_rejected_while_buy_is_unresolved(self):
        state = self.buy(qty=20, filled=10)
        self.assertTrue(state.pending)
        with self.assertRaisesRegex(ValueError, "PENDING_BUY_RECONCILIATION"):
            self.book.record_hypothetical_sell("s", "SPY", 1, D("50"))
        self.assertEqual(self.book.snapshot(), state)

    def test_oversell_duplicate_conflict_and_fee_veto_roll_back(self):
        state = self.buy()
        for fill_id, sym, qty, price, fee, error in (
            ("oversell", "SPY", 21, D("50"), D("0"), "SYNTHETIC_OVERSELL"),
            ("wrong", "QQQ", 1, D("50"), D("0"), "SYNTHETIC_OVERSELL"),
            ("fee", "SPY", 1, D("50"), D("51"), "SELL_FEE_EXCEEDS_PROCEEDS"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.book.record_hypothetical_sell(fill_id, sym, qty, price, fee=fee)
            self.assertEqual(self.book.snapshot(), state)
        applied = self.book.record_hypothetical_sell("once", "SPY", 1, D("50"))
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SELL_FILL_ID"):
            self.book.record_hypothetical_sell("once", "SPY", 1, D("51"))
        self.assertEqual(self.book.snapshot(), applied)

    def test_no_new_buys_after_sell_in_bounded_phase(self):
        self.buy()
        after_sell = self.book.record_hypothetical_sell("s", "SPY", 1, D("50"))
        with self.assertRaisesRegex(ValueError, "BUY_AFTER_SELL_UNSUPPORTED"):
            self.book.reserve_buy("another", account(cash=after_sell.cash),
                                  proposal(D("1")), reconciled=True)
        self.assertEqual(self.book.snapshot(), after_sell)

    def test_corrupt_sell_receipt_rejected_on_restart(self):
        self.buy()
        before = self.book.record_hypothetical_sell("s", "SPY", 10, D("50"))
        self.book.close()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE hypothetical_sells SET qty=25 WHERE id='s'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id=SESSION)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE hypothetical_sells SET qty=10 WHERE id='s'")
        self.book = OfflineReservations(self.path, session_id=SESSION)
        self.assertEqual(self.book.snapshot(), before)

    def test_forced_sqlite_sell_cash_update_failure_rolls_back_receipt(self):
        bought = self.buy()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER reject_sell_cash
                BEFORE UPDATE OF value ON book_meta
                WHEN NEW.key='cash' AND EXISTS(SELECT 1 FROM hypothetical_sells)
                BEGIN SELECT RAISE(ABORT, 'synthetic cash update failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.record_hypothetical_sell(
                "sell-rollback", "SPY", 10, D("50"))
        self.assertEqual(self.book.snapshot(), bought)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hypothetical_sells").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_sell_cash")
        self.book.close()
        self.book = OfflineReservations(self.path, session_id=SESSION)
        self.assertEqual(self.book.snapshot(), bought)
        recovered = self.book.record_hypothetical_sell(
            "sell-rollback", "SPY", 10, D("50"))
        self.assertEqual(recovered.cash, D("99520"))

    def test_halt_blocks_new_synthetic_sell_receipts(self):
        self.buy()
        self.book.halt()
        stopped = self.book.snapshot()
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_sell("s", "SPY", 1, D("50"))
        self.assertEqual(self.book.snapshot(), stopped)

    def test_no_broker_entrypoints_exist(self):
        import alphora.reservations as mod
        for name in ("send_order", "submit_order", "connect_alpaca"):
            self.assertFalse(hasattr(mod, name))


if __name__ == "__main__":
    unittest.main()
