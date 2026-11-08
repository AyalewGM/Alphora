"""Deterministic synthetic-only sell-replay acceptance tests."""
import unittest
from dataclasses import replace
from decimal import Decimal as D

from alphora.offline_exit_projection import (
    PositionCost, SyntheticSellReceipt, project_synthetic_sells,
)

SESSION = "2026-11-06"
OPENING = (PositionCost("SPY", 100, D("5010")), PositionCost("QQQ", 20, D("2000")))
CASH = D("92990")


def receipt(**changes):
    return replace(SyntheticSellReceipt("fill-1", 1, "SPY", 40, D("48"), D("2")), **changes)


def project(*events, opening=OPENING, cash=CASH, **kw):
    return project_synthetic_sells(SESSION, cash, opening, tuple(events), **kw)


class OfflineExitProjectionTests(unittest.TestCase):
    def test_empty_and_hash_stability(self):
        a = project()
        b = project()
        self.assertEqual(a, b)
        self.assertEqual(a.cash, CASH)
        self.assertEqual(a.realized_pnl, D("0"))
        self.assertEqual(a.applied_receipts, 0)
        self.assertEqual(project(expected_sha256=a.receipt_sha256), a)

    def test_partial_sell_releases_average_basis_and_fee(self):
        result = project(receipt())
        self.assertEqual(result.cash, D("94908"))
        self.assertEqual(result.realized_pnl, D("-86"))
        self.assertEqual(result.positions[1], PositionCost("SPY", 60, D("3006")))
        self.assertEqual(result.applied_receipts, 1)

    def test_full_liquidation_and_loss_beyond_stop_is_not_hidden(self):
        result = project(receipt(shares=100, price=D("30"), fee=D("5")))
        self.assertEqual(result.realized_pnl, D("-2015"))
        self.assertEqual(result.cash, D("95985"))
        self.assertEqual(result.positions, (PositionCost("QQQ", 20, D("2000")),))

    def test_partial_then_final_sale_conserves_cost_basis(self):
        open_lots = (PositionCost("SPY", 3, D("100")),)
        first = SyntheticSellReceipt("a", 1, "SPY", 1, D("40"))
        second = SyntheticSellReceipt("b", 2, "SPY", 2, D("40"))
        result = project(first, second, opening=open_lots, cash=D("99900"))
        self.assertEqual(result.cash, D("100020"))
        self.assertEqual(result.realized_pnl, D("20"))
        self.assertEqual(result.positions, ())

    def test_exact_duplicate_receipt_deduped(self):
        self.assertEqual(project(receipt(), receipt()), project(receipt()))
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SELL_RECEIPT_ID"):
            project(receipt(), receipt(price=D("49")))
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SELL_RECEIPT_ID"):
            project(receipt(), receipt(sequence=2))

    def test_sequence_gaps_and_reordering_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "SELL_RECEIPT_SEQUENCE_GAP"):
            project(receipt(sequence=2))
        with self.assertRaisesRegex(ValueError, "SELL_RECEIPT_SEQUENCE_GAP"):
            project(receipt(), receipt(receipt_id="fill-2"))
        with self.assertRaisesRegex(ValueError, "SELL_RECEIPT_SEQUENCE_GAP"):
            project(receipt(receipt_id="fill-2", sequence=2), receipt())

    def test_oversell_and_unheld_symbol_rejected(self):
        for bad in (receipt(shares=101), receipt(symbol="IWM")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "SYNTHETIC_OVERSELL"):
                project(bad)
        with self.assertRaisesRegex(ValueError, "SYNTHETIC_OVERSELL"):
            project(receipt(shares=60), receipt(receipt_id="fill-2", sequence=2, shares=41))

    def test_oversized_opening_inventory_fails_before_scan(self):
        # Reject an oversized caller-attested snapshot before iterating or
        # hashing repeated positions; this is a bounded reference replay.
        oversized = (PositionCost("SPY", 1, D("0.01")),) * 10001
        with self.assertRaisesRegex(ValueError, "INVALID_EXIT_REPLAY_INPUT"):
            project(opening=oversized)

    def test_unbalanced_book_and_invalid_positions_rejected(self):
        for positions in ((PositionCost("SPY", 100, D("5010")),),
                          OPENING + (OPENING[0],),
                          (PositionCost("SPY", True, D("5010")),),
                          (PositionCost("SPY", 1, D("NaN")),)):
            with self.subTest(positions=positions), self.assertRaises(ValueError):
                project(opening=positions)
        with self.assertRaisesRegex(ValueError, "INVALID_EXIT_REPLAY_INPUT"):
            project(cash=D("Inf"))

    def test_cannot_sneak_invalid_receipt_values_into_replay(self):
        variants = (receipt(shares=True), receipt(price=D("NaN")),
                    receipt(price=D("1.001")), receipt(fee=D("-1")),
                    receipt(sequence=True), receipt(receipt_id=""),
                    receipt(symbol="spy"), receipt(fee=D("0.001")))
        for item in variants:
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "INVALID_SYNTHETIC_SELL_RECEIPT"):
                project(item)
        with self.assertRaisesRegex(ValueError, "INVALID_SYNTHETIC_SELL_VALUE"):
            project(receipt(price=D("1"), fee=D("100")))

    def test_no_hindsight_hash_replacement_and_order_independence_of_opening(self):
        expected = project(receipt()).receipt_sha256
        self.assertEqual(project(receipt(), opening=tuple(reversed(OPENING)),
                                 expected_sha256=expected).receipt_sha256, expected)
        with self.assertRaisesRegex(ValueError, "SELL_RECEIPT_HASH_MISMATCH"):
            project(receipt(price=D("48.01")), expected_sha256=expected)
        with self.assertRaisesRegex(ValueError, "INVALID_EXIT_REPLAY_INPUT"):
            project(expected_sha256="not-a-sha")

    def test_invalid_session_and_input_types(self):
        for bad in ("2026-02-30", "2026-1-1", "2026-11-06z"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "INVALID_EXIT_REPLAY_INPUT"):
                project_synthetic_sells(bad, CASH, OPENING, ())
        with self.assertRaisesRegex(ValueError, "INVALID_EXIT_REPLAY_INPUT"):
            project_synthetic_sells(SESSION, CASH, list(OPENING), ())
        with self.assertRaisesRegex(ValueError, "INVALID_EXIT_REPLAY_INPUT"):
            project_synthetic_sells(SESSION, CASH, OPENING, [receipt()])

    def test_no_broker_or_orders_surface(self):
        import alphora.offline_exit_projection as mod
        self.assertFalse(hasattr(mod, "submit_order"))
        self.assertFalse(hasattr(mod, "connect_alpaca"))
        self.assertFalse(hasattr(mod, "place_order"))


if __name__ == "__main__":
    unittest.main()
