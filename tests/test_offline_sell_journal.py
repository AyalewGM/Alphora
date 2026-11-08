"""Offline synthetic SELL journal recovery and atomicity tests (no broker)."""
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from alphora.offline_exit_projection import PositionCost, SyntheticSellReceipt
from alphora.offline_sell_journal import OfflineSellJournal
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount

SESSION = "2026-11-06"
OPENING = (PositionCost("SPY", 100, D("5010")),
           PositionCost("QQQ", 20, D("2000")))
CASH = D("92990")


def sell(**kwargs):
    base = SyntheticSellReceipt("synthetic-fill-1", 1, "SPY", 40, D("48"), D("2"))
    return replace(base, **kwargs)


class SellJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "receipts.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, *, cash=CASH, positions=OPENING, session=SESSION):
        return OfflineSellJournal(
            self.path, session_id=session, opening_cash=cash,
            opening_positions=positions)

    def test_empty_journal_and_restart_replay(self):
        with self.open() as book:
            empty = book.snapshot()
            self.assertEqual(empty.cash, CASH)
            expected = book.append(sell())
            self.assertEqual(expected.cash, D("94908"))
            self.assertEqual(expected.realized_pnl, D("-86"))
            self.assertEqual(expected.applied_receipts, 1)
        with self.open() as reopened:
            self.assertEqual(reopened.snapshot(), expected)
            self.assertEqual(reopened.append(sell()), expected)
            self.assertEqual(reopened.snapshot().applied_receipts, 1)

    def test_partial_then_complete_sell_and_restart(self):
        with self.open() as book:
            book.append(sell())
            outcome = book.append(SyntheticSellReceipt(
                "synthetic-fill-2", 2, "SPY", 60, D("50"), D("3")))
            self.assertEqual(outcome.positions,
                             (PositionCost("QQQ", 20, D("2000")),))
            self.assertEqual(outcome.cash, D("97905"))
            self.assertEqual(outcome.applied_receipts, 2)
        with self.open() as reopened:
            self.assertEqual(reopened.snapshot(), outcome)

    def test_conflict_and_gap_never_change_persistent_book(self):
        with self.open() as book:
            expected = book.append(sell())
            for receipt, error in (
                (sell(price=D("49")), "CONFLICTING_SELL_RECEIPT_ID"),
                (sell(receipt_id="synthetic-fill-2", sequence=3),
                 "SELL_RECEIPT_SEQUENCE_GAP"),
                (sell(receipt_id="synthetic-fill-2", sequence=2,
                      shares=1000), "SYNTHETIC_OVERSELL"),
            ):
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                    book.append(receipt)
                self.assertEqual(book.snapshot(), expected)
        with self.open() as reopened:
            self.assertEqual(reopened.snapshot(), expected)

    def test_opening_identity_and_session_must_match(self):
        with self.open():
            pass
        with self.assertRaisesRegex(ValueError, "SELL_JOURNAL_OPENING_MISMATCH"):
            self.open(session="2026-11-07")
        other = (PositionCost("SPY", 100, D("5010")),
                 PositionCost("QQQ", 20, D("1990")))
        with self.assertRaisesRegex(ValueError, "SELL_JOURNAL_OPENING_MISMATCH"):
            self.open(cash=D("93000"), positions=other)
        with self.open() as reopened:
            self.assertEqual(reopened.snapshot().cash, CASH)

    def test_corrupted_rows_are_rejected_after_restart(self):
        with self.open() as book:
            book.append(sell())
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE synthetic_sells SET shares=1000 WHERE receipt_id=?",
                       ("synthetic-fill-1",))
        with self.assertRaisesRegex(ValueError, "SELL_JOURNAL_CORRUPT"):
            self.open()

    def _completed_buy_book(self):
        source = OfflineReservations(self.path.parent / "buy.sqlite",
                                     session_id=SESSION)
        account = PaperAccount(D("100000"), D("100000"),
                               D("100000"), D("0"), SESSION)
        proposal = BuyProposal("SPY", D("20"), D("50"), D("48"))
        source.reserve_buy("buy-1", account, proposal, reconciled=True)
        return source

    def test_bridge_from_completed_buy_book_and_restart(self):
        source = self._completed_buy_book()
        try:
            pending = source.snapshot()
            with self.assertRaisesRegex(ValueError, "UNRECONCILED_BUY_BOOK"):
                OfflineSellJournal.from_buy_snapshot(
                    self.path, pending, reconciled=True)
            self.assertFalse(self.path.exists())
            filled = source.record_hypothetical_fill(
                "buy-1", "buy-fill-1", 20, D("49"))
            self.assertEqual(filled.cash, D("99020"))
            self.assertEqual(filled.positions, (("SPY", 20, D("980")),))
            with OfflineSellJournal.from_buy_snapshot(
                    self.path, filled, reconciled=True) as sell_book:
                initial = sell_book.snapshot()
                self.assertEqual(initial.cash, D("99020"))
                self.assertEqual(initial.positions,
                                 (PositionCost("SPY", 20, D("980")),))
                after = sell_book.append(SyntheticSellReceipt(
                    "sell-fill-1", 1, "SPY", 10, D("50")))
                self.assertEqual(after.cash, D("99520"))
                self.assertEqual(after.realized_pnl, D("10"))
                self.assertEqual(after.positions,
                                 (PositionCost("SPY", 10, D("490")),))
                self.assertEqual(source.snapshot(), filled)
            with OfflineSellJournal.from_buy_snapshot(
                    self.path, filled, reconciled=True) as recovered:
                self.assertEqual(recovered.snapshot(), after)
                self.assertEqual(recovered.append(SyntheticSellReceipt(
                    "sell-fill-1", 1, "SPY", 10, D("50"))), after)
        finally:
            source.close()

    def test_bridge_refuses_untrusted_or_inconsistent_buy_snapshot(self):
        source = self._completed_buy_book()
        try:
            filled = source.record_hypothetical_fill(
                "buy-1", "buy-fill-1", 20, D("49"))
            variants = (
                replace(filled, reserved_cash=D("1")),
                replace(filled, available_cash=D("99019")),
                replace(filled, halted=True),
                replace(filled, pending=(("fake", "SPY", 1, D("50")),)),
                replace(filled, positions=(("SPY", 20, D("979")),)),
                replace(filled, positions=(("SPY", 20, D("980")),
                                           ("SPY", 1, D("1")))),
            )
            for bad in variants:
                with self.subTest(bad=bad), self.assertRaisesRegex(
                        ValueError, "UNRECONCILED_BUY_BOOK"):
                    OfflineSellJournal.from_buy_snapshot(
                        self.path, bad, reconciled=True)
            with self.assertRaisesRegex(ValueError, "UNRECONCILED_BUY_BOOK"):
                OfflineSellJournal.from_buy_snapshot(
                    self.path, filled, reconciled=False)
            with self.assertRaisesRegex(ValueError, "UNRECONCILED_BUY_BOOK"):
                OfflineSellJournal.from_buy_snapshot(
                    self.path, None, reconciled=True)
            self.assertFalse(self.path.exists())
        finally:
            source.close()

    def test_invalid_input_and_no_broker_surface(self):
        with self.assertRaisesRegex(ValueError, "INVALID_JOURNAL_PATH"):
            OfflineSellJournal(":memory:", session_id=SESSION,
                               opening_cash=CASH, opening_positions=OPENING)
        with self.open() as book:
            with self.assertRaisesRegex(ValueError, "INVALID_SYNTHETIC_SELL_RECEIPT"):
                book.append(None)
            self.assertEqual(book.snapshot().applied_receipts, 0)
        import alphora.offline_sell_journal as mod
        for name in ("connect_alpaca", "submit_order", "place_order", "send_order"):
            self.assertFalse(hasattr(mod, name))


if __name__ == "__main__":
    unittest.main()
