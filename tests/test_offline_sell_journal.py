"""Offline synthetic SELL journal recovery and atomicity tests (no broker)."""
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from alphora.offline_exit_projection import PositionCost, SyntheticSellReceipt
from alphora.offline_sell_journal import OfflineSellJournal

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
