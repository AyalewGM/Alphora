import unittest
from dataclasses import replace
from decimal import Decimal as D
from alphora.risk import PaperAccount, BuyProposal, validate_buy, INITIAL_EQUITY, REBALANCE_FREQUENCY


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
