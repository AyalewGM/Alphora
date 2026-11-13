"""Bounded operator-configurable PAPER risk policy regression tests.

No broker credentials, live orders, or automated trading.
"""
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from alphora.risk import (
    BuyProposal, PaperAccount, RiskPolicy, DEFAULT_RISK_POLICY,
    validate_buy,
)
from alphora.decision import EntryCandidate, propose_entry
from alphora.reservations import OfflineReservations
from datetime import datetime, timezone


SESSION = "2026-11-02"


def account():
    return PaperAccount(D("100000"), D("100000"), D("100000"), D("0"), SESSION)


class ConfigurableRiskTests(unittest.TestCase):
    def test_default_policy_preserves_approved_baseline(self):
        self.assertEqual(DEFAULT_RISK_POLICY.risk_per_trade_fraction, D("0.005"))
        self.assertEqual(DEFAULT_RISK_POLICY.max_instrument_fraction, D("0.10"))
        proposal = BuyProposal("SPY", D("20"), D("500"), D("475"))
        self.assertTrue(validate_buy(account(), proposal,
                                     expected_session_id=SESSION).permitted)

    def test_explicit_tighter_only_policy_affects_risk_guard(self):
        # Approved ceilings are hard maximums. A caller may tighten them,
        # not authorize a $20k instrument or $1k planned loss.
        proposal = BuyProposal("SPY", D("20"), D("500"), D("475"))
        self.assertTrue(validate_buy(
            account(), proposal, expected_session_id=SESSION).permitted)
        self.assertEqual(validate_buy(account(), proposal,
            expected_session_id=SESSION,
            policy=RiskPolicy(D("0.0025"), D("0.10"))).reason,
            "TRADE_RISK_EXCEEDED")
        self.assertEqual(validate_buy(account(), proposal,
            expected_session_id=SESSION,
            policy=RiskPolicy(D("0.005"), D("0.05"))).reason,
            "INSTRUMENT_ALLOCATION_EXCEEDED")
        with self.assertRaisesRegex(ValueError, "INVALID_RISK_POLICY"):
            RiskPolicy(D("0.01"), D("0.20"))

    def test_malformed_or_out_of_range_policies_fail_closed(self):
        for risk, alloc in [
            (D("0"), D("0.1")), (D("0.021"), D("0.1")),
            (D("NaN"), D("0.1")), (D("0.005"), D("0")),
            (D("0.005"), D("1.01")), (D("0.005"), D("Infinity")),
            (D("0.01"), D("0.10")), (D("0.005"), D("0.20")),
            (0.005, D("0.1")), (D("0.005"), 0.1),
        ]:
            with self.subTest(risk=str(risk), allocation=str(alloc)):
                with self.assertRaisesRegex(ValueError, "INVALID_RISK_POLICY"):
                    RiskPolicy(risk, alloc)
        proposal = BuyProposal("SPY", D("1"), D("500"), D("475"))
        self.assertEqual(validate_buy(account(), proposal,
            expected_session_id=SESSION, policy=None).reason, "INVALID_RISK_POLICY")
        self.assertEqual(validate_buy(account(), proposal,
            expected_session_id=SESSION, mode="live",
            policy=RiskPolicy(D("0.005"), D("0.1"))).reason, "REAL_TRADING_DISABLED")

    def test_entry_sizing_uses_same_policy_as_final_guard(self):
        now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
        c = EntryCandidate("SPY", D("50"), D("48"), now,
                           "WATCHLIST", "VERIFIED", D("50"), "SPY", True)
        kwargs = dict(as_of=now, expected_session_id=SESSION,
                      reserved_cash=D("0"), reconciled=True)
        self.assertEqual(propose_entry(account(), c, **kwargs).proposal.quantity, D("200"))
        tighter = RiskPolicy(D("0.0025"), D("0.05"))
        self.assertEqual(propose_entry(account(), c, policy=tighter,
                                       **kwargs).proposal.quantity, D("100"))
        with self.assertRaisesRegex(ValueError, "INVALID_RISK_POLICY"):
            RiskPolicy(D("0.01"), D("0.20"))
        self.assertFalse(propose_entry(account(), c, policy=None, **kwargs).permitted)

    def test_ledger_policy_is_pinned_across_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.sqlite"
            policy = RiskPolicy(D("0.0025"), D("0.05"))
            proposal = BuyProposal("SPY", D("10"), D("500"), D("475"))
            with_policy = OfflineReservations(path, session_id=SESSION, policy=policy)
            reserved = with_policy.reserve_buy("large-paper", account(),
                                               proposal, reconciled=True)
            self.assertEqual(reserved.reserved_cash, D("5000"))
            with_policy.close()
            with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
                OfflineReservations(path, session_id=SESSION)
            reopened = OfflineReservations(path, session_id=SESSION,
                                          policy=RiskPolicy(D("0.002500"), D("0.0500")))
            self.assertEqual(reopened.snapshot(), reserved)
            reopened.close()

    def test_default_book_still_rejects_above_approved_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.sqlite"
            book = OfflineReservations(path, session_id=SESSION)
            proposal = BuyProposal("SPY", D("40"), D("500"), D("475"))
            with self.assertRaisesRegex(ValueError, "RISK_VETO:TRADE_RISK_EXCEEDED"):
                book.reserve_buy("large-paper", account(), proposal, reconciled=True)
            self.assertEqual(book.snapshot().reserved_cash, D("0"))
            book.close()


if __name__ == "__main__":
    unittest.main()
