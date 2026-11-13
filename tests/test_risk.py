import unittest
from dataclasses import replace
from decimal import Decimal as D, localcontext
from alphora.risk import PaperAccount, BuyProposal, RiskPolicy, validate_buy, INITIAL_EQUITY, REBALANCE_FREQUENCY


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.account = PaperAccount(D("100000"), D("100000"), D("100000"), D("0"), "2026-11-02")
        self.buy = BuyProposal("SPY", D("20"), D("500"), D("475"))

    def decide(self, account=None, proposal=None, **kwargs):
        return validate_buy(account or self.account, proposal or self.buy,
                            expected_session_id="2026-11-02", **kwargs)

    def test_config(self):
        self.assertEqual(INITIAL_EQUITY, D("100000"))
        self.assertEqual(REBALANCE_FREQUENCY, "daily")

    def test_policy_fingerprint_is_exact_under_ambient_decimal_precision(self):
        # A persisted risk-policy identity must distinguish even a tiny
        # decrease in the allowed per-trade loss (still below hard cap). Ambient precision=3
        # previously rounded both identities to the same "0.005|0.1".
        original = RiskPolicy(D("0.005"), D("0.10"))
        tightened = RiskPolicy(D("0.00499999999999999999999999"), D("0.10"))
        equivalent = RiskPolicy(D("0.00500"), D("0.1000"))
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                self.assertEqual(original.fingerprint(), "0.005|0.1")
                self.assertEqual(equivalent.fingerprint(), original.fingerprint())
                self.assertEqual(
                    tightened.fingerprint(), "0.00499999999999999999999999|0.1")
                self.assertNotEqual(tightened.fingerprint(), original.fingerprint())

    def test_paper_policy_never_loosens_approved_hard_limits(self):
        # Caller-provided policies can tighten but never exceed the
        # owner's 0.5% per-trade and 10% single-instrument ceilings.
        for risk, allocation in (
            (D("0.02"), D("0.10")),
            (D("0.005"), D("1")),
            (D("0.00500000000000000000000001"), D("0.10")),
            (D("0.005"), D("0.10000000000000000000000001")),
        ):
            with self.subTest(risk=risk, allocation=allocation):
                with self.assertRaisesRegex(ValueError, "INVALID_RISK_POLICY"):
                    RiskPolicy(risk, allocation)
        self.assertEqual(RiskPolicy(), RiskPolicy(D("0.005"), D("0.10")))
        tightened = RiskPolicy(D("0.0025"), D("0.05"))
        self.assertEqual(tightened.risk_per_trade_fraction, D("0.0025"))
        self.assertEqual(tightened.max_instrument_fraction, D("0.05"))
        self.assertTrue(self.decide().permitted)

    def test_boundary_500_risk_and_10000_allocation(self):
        self.assertTrue(self.decide().permitted)

    def test_risk_exceeds_500(self):
        self.assertEqual(self.decide(proposal=replace(self.buy, stop_price=D("474"))).reason,
                         "TRADE_RISK_EXCEEDED")

    def test_allocation_exceeds_10000(self):
        self.assertEqual(self.decide(proposal=replace(self.buy, existing_instrument_value=D("1"))).reason,
                         "INSTRUMENT_ALLOCATION_EXCEEDED")

    def test_daily_loss_boundary_2000(self):
        self.assertEqual(self.decide(account=replace(self.account, day_pnl=D("-2000"))).reason,
                         "DAILY_LOSS_HALT")

    def test_direct_guard_uses_current_session_open_not_initial_equity(self):
        # Issue #42: these are synthetic *direct guard* contracts only.
        # The runner/book currently reject cross-session rollover; this test
        # does not authorize or simulate durable multi-session accounting.
        tiny_order = BuyProposal("SPY", D("1"), D("100"), D("99"))
        # Prior session closes/next session opens at $105k. A $3k current-day
        # drawdown is >2% even though equity remains above the initial $100k.
        # A $1,900 drawdown from a $95k open is exactly 2%.
        for opening, mark in (
            ("105000", "102000"),
            ("105000", "102900"),
            ("95000", "93100"),
        ):
            with self.subTest(opening=opening, mark=mark):
                actual_pnl = D(mark) - D(opening)
                account = replace(self.account,
                    session_open_equity=D(opening), equity=D(mark),
                    cash=D(mark), day_pnl=actual_pnl)
                self.assertEqual(self.decide(account=account,
                    proposal=tiny_order).reason, "DAILY_LOSS_HALT")
                # A caller-supplied stale or falsified P&L must not override
                # the equity-versus-session-open loss calculation.
                spoofed = replace(account, day_pnl=D("0"))
                self.assertEqual(self.decide(account=spoofed,
                    proposal=tiny_order).reason, "DAILY_LOSS_HALT")

        # One cent better than the 2% boundary is not itself a loss halt.
        # These are single synthetic account snapshots, NOT rollover tests.
        for opening, mark in (
            ("105000", "102900.01"),
            ("95000", "93100.01"),
        ):
            with self.subTest(opening=opening, mark=mark):
                account = replace(self.account,
                    session_open_equity=D(opening), equity=D(mark),
                    cash=D(mark), day_pnl=D(mark) - D(opening))
                self.assertTrue(self.decide(account=account,
                    proposal=tiny_order).permitted)

    def test_below_daily_loss_boundary(self):
        account=replace(self.account, equity=D("98001"), cash=D("98001"), day_pnl=D("-1999"))
        proposal=replace(self.buy, quantity=D("19"))
        self.assertTrue(self.decide(account=account, proposal=proposal).permitted)

    def test_persistent_halt(self):
        self.assertEqual(self.decide(account=replace(self.account, halted=True)).reason,
                         "DAILY_LOSS_HALT")

    def test_invalid_halt_state_fails_closed(self):
        # A malformed persisted/reconciled kill-switch value must not be
        # treated as False merely because it is falsey in Python.
        for halted in (None, 0, 1, "", D("0")):
            with self.subTest(halted=halted):
                account = replace(self.account, halted=halted)
                self.assertEqual(self.decide(account=account).reason,
                                 "INVALID_ACCOUNT_STATE")
        self.assertTrue(self.decide(account=replace(self.account, halted=False)).permitted)

    def test_untyped_account_or_proposal_fails_closed(self):
        self.assertEqual(validate_buy(None, self.buy,
                                      expected_session_id="2026-11-02").reason,
                         "INVALID_ACCOUNT_OR_ORDER")
        self.assertEqual(validate_buy(self.account, None,
                                      expected_session_id="2026-11-02").reason,
                         "INVALID_ACCOUNT_OR_ORDER")

    def test_direct_risk_decision_is_independent_of_caller_decimal_precision(self):
        # A realistic cent-priced, whole-share $1,646.04 order is inside
        # the $500 risk, $10,000 symbol cap and $100k available cash.
        # Ambient precision 3 must not turn it into a spurious veto.
        proposal = BuyProposal("SPY", D("33"), D("49.88"), D("48.00"))
        for prec in (3, 6, 28, 80):
            with self.subTest(precision=prec), localcontext() as ctx:
                ctx.prec = prec
                decision = self.decide(proposal=proposal)
                self.assertEqual((decision.permitted, decision.reason),
                                 (True, "PAPER_BUY_ALLOWED"))

        # The safety veto must be just as stable; legitimate near-boundary
        # overallocations are not permitted or rounded to the 10% cap.
        over = BuyProposal("SPY", D("20"),
                           D("500.00000000000000000000000001"),
                           D("475.00000000000000000000000001"))
        for prec in (3, 6, 28, 80):
            with self.subTest(precision=prec), localcontext() as ctx:
                ctx.prec = prec
                decision = self.decide(proposal=over)
                self.assertFalse(decision.permitted)

    def test_inexact_risk_boundary_arithmetic(self):
        # True notional exceeds the 10% $10,000 cap by a tiny fraction.
        # Pinned 50-digit precision preserves the overage and veto reason.
        hidden_notional = replace(
            self.buy,
            price=D("500.00000000000000000000000001"),
            stop_price=D("475.00000000000000000000000001"),
        )
        self.assertEqual(self.decide(proposal=hidden_notional).reason,
                         "INSTRUMENT_ALLOCATION_EXCEEDED")
        # Likewise the existing exposure must not silently disappear when
        # summing its value with the full $10,000 proposed notional.
        hidden_position = replace(
            self.buy, existing_instrument_value=D("0.0000000000000000000000000001"),
        )
        self.assertEqual(self.decide(proposal=hidden_position).reason,
                         "INSTRUMENT_ALLOCATION_EXCEEDED")
        # A stop just below $475 makes true planned loss greater than $500;
        # the direct risk boundary must preserve and classify this overage.
        hidden_risk = replace(
            self.buy, stop_price=D("474.999999999999999999999999999"),
        )
        self.assertEqual(self.decide(proposal=hidden_risk).reason,
                         "TRADE_RISK_EXCEEDED")
        # Ordinary exact boundary cases must remain accepted.
        self.assertTrue(self.decide().permitted)

    def test_extreme_decimals_fail_closed(self):
        # Decimal accepts finite exponents beyond the active arithmetic
        # context; neither a calculation overflow nor a rounded-zero
        # notional may become a permitted paper buy.
        for proposal in (
            replace(self.buy, quantity=D("1e1000000")),
            replace(self.buy, price=D("1e1000000")),
            replace(self.buy, price=D("1e-10000000"),
                    stop_price=D("5e-10000001")),
        ):
            with self.subTest(proposal=proposal):
                decision = self.decide(proposal=proposal)
                self.assertFalse(decision.permitted)
                self.assertEqual(decision.reason, "INVALID_ACCOUNT_OR_ORDER")
        account = replace(self.account, equity=D("1e1000000"))
        decision = self.decide(account=account)
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.reason, "INVALID_ACCOUNT_OR_ORDER")

    def test_bad_session(self):
        self.assertEqual(validate_buy(self.account, self.buy, expected_session_id="wrong").reason,
                         "SESSION_MISMATCH")

    def test_live_mode_rejected(self):
        self.assertEqual(self.decide(mode="live").reason, "REAL_TRADING_DISABLED")

    def test_insufficient_cash(self):
        self.assertEqual(self.decide(account=replace(self.account, cash=D("9999"))).reason,
                         "INSUFFICIENT_CASH")

    def test_invalid_stop(self):
        self.assertEqual(self.decide(proposal=replace(self.buy, stop_price=D("501"))).reason,
                         "INVALID_STOP")

    def test_negative_qty(self):
        self.assertEqual(self.decide(proposal=replace(self.buy, quantity=D("-1"))).reason,
                         "INVALID_ACCOUNT_OR_ORDER")

    def test_nan_fails_closed(self):
        self.assertEqual(self.decide(proposal=replace(self.buy, price=D("NaN"))).reason,
                         "INVALID_ACCOUNT_OR_ORDER")

    def test_bad_symbol(self):
        self.assertEqual(self.decide(proposal=replace(self.buy, symbol="SPY;DROP")).reason,
                         "INVALID_SYMBOL")

    def test_direct_guard_symbol_matches_offline_book(self):
        # Previously punctuation-only and oversized names passed the
        # low-level risk veto, even though the durable ledger rejected them.
        for symbol in ("---", "...", "1SPY", "A" * 11):
            with self.subTest(symbol=symbol):
                self.assertEqual(self.decide(proposal=replace(self.buy, symbol=symbol)).reason,
                                 "INVALID_SYMBOL")
        self.assertTrue(self.decide(proposal=replace(self.buy, symbol="BRK.B")).permitted)

    def test_cash_only(self):
        self.assertEqual(self.decide(account=replace(self.account, cash=D("-1"))).reason,
                         "INVALID_ACCOUNT_STATE")

    def test_direct_risk_guard_rejects_fractional_shares(self):
        # Proposals must remain whole-share even if callers bypass propose_entry.
        for qty in (D("0.5"), D("1.5"), D("19.999")):
            with self.subTest(quantity=qty):
                proposal = replace(self.buy, quantity=qty)
                self.assertEqual(self.decide(proposal=proposal).reason,
                                 "INVALID_ACCOUNT_OR_ORDER")
        self.assertTrue(self.decide().permitted)

    def test_direct_risk_guard_rejects_nonstring_symbol(self):
        for symbol in (123, True, None):
            with self.subTest(symbol=symbol):
                proposal = replace(self.buy, symbol=symbol)
                self.assertEqual(self.decide(proposal=proposal).reason,
                                 "INVALID_SYMBOL")

    def test_direct_risk_guard_rejects_nonstring_session_ids(self):
        numeric_session = replace(self.account, session_id=20261102)
        self.assertEqual(
            validate_buy(numeric_session, self.buy, expected_session_id=20261102).reason,
            "SESSION_MISMATCH",
        )
        empty_session = replace(self.account, session_id="")
        self.assertEqual(
            validate_buy(empty_session, self.buy, expected_session_id="").reason,
            "SESSION_MISMATCH",
        )

    def test_inconsistent_cash_and_pnl_fail_closed(self):
        a=replace(self.account,equity=D("99500"),cash=D("99500"),day_pnl=D(0))
        self.assertEqual(self.decide(account=a).reason,"INCONSISTENT_ACCOUNT_STATE")
        a=replace(self.account,cash=D("100001"))
        self.assertEqual(self.decide(account=a).reason,"INCONSISTENT_ACCOUNT_STATE")

    def test_equity_drift_enforces_daily_halt(self):
        a=replace(self.account,equity=D("95000"),cash=D("95000"),day_pnl=D(0))
        self.assertEqual(self.decide(account=a).reason,"DAILY_LOSS_HALT")


if __name__ == "__main__":
    unittest.main()
