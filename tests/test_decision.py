import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from alphora.risk import PaperAccount
from alphora.decision import (EntryCandidate, FilledPosition, ExitQuote,
                              propose_entry, propose_exit)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
        self.account = PaperAccount(D('100000'), D('100000'), D('100000'), D('0'), '2026-11-02')
        self.candidate = EntryCandidate('SPY', D('50'), D('48'), self.now,
                                        'WATCHLIST', 'VERIFIED')
        self.position = FilledPosition('paper-pos-1', 'SPY', D('100'), D('50'), D('48'))
        self.quote = ExitQuote(D('51.50'), D('51.52'), self.now, True)

    def entry(self, candidate=None, account=None, **changes):
        options = {'as_of': self.now, 'expected_session_id': '2026-11-02',
                   'reserved_cash': D(0), 'reconciled': True}
        options.update(changes)
        return propose_entry(account or self.account, candidate or self.candidate, **options)

    def exit(self, position=None, quote=None, **changes):
        options = {'as_of': self.now, 'take_profit_pct': D(3)}
        options.update(changes)
        return propose_exit(position or self.position, quote or self.quote, **options)

    def test_allocation_caps_200_shares(self):
        plan=self.entry()
        self.assertTrue(plan.permitted)
        self.assertEqual(plan.proposal.quantity, D(200))
        self.assertEqual(plan.proposal.price * plan.proposal.quantity, D(10000))

    def test_planned_risk_caps_quantity(self):
        plan=self.entry(replace(self.candidate, stop_price=D(40)))
        self.assertEqual(plan.proposal.quantity, D(50))

    def test_cash_reservation_across_pending_orders(self):
        plan=self.entry(reserved_cash=D(97000))
        self.assertEqual(plan.proposal.quantity, D(60))

    def test_instrument_exposure_includes_pending(self):
        c=replace(self.candidate, existing_instrument_value=D(8000),
                  pending_instrument_value=D(1000))
        self.assertEqual(self.entry(c).proposal.quantity, D(20))

    def test_overcommitted_cash_rejected(self):
        self.assertEqual(self.entry(reserved_cash=D(100001)).reason, 'OVERCOMMITTED_CASH')

    def test_unreconciled_fail_closed(self):
        self.assertEqual(self.entry(reconciled=False).reason, 'UNRECONCILED_STATE')

    def test_caution_or_unknown_catalyst_rejected(self):
        self.assertEqual(self.entry(replace(self.candidate, scanner_classification='CAUTION')).reason,
                         'SCANNER_NOT_ELIGIBLE')
        self.assertEqual(self.entry(replace(self.candidate, catalyst_status='UNKNOWN')).reason,
                         'CATALYST_REVIEW_REQUIRED')

    def test_non_boolean_reconciliation_and_bad_symbol_fail_closed(self):
        self.assertEqual(self.entry(reconciled="true").reason, "UNRECONCILED_STATE")
        self.assertEqual(self.entry(replace(self.candidate, symbol=12)).reason, "INVALID_SYMBOL")

    def test_quote_stale_or_future_rejected(self):
        stale=replace(self.candidate, observed_at=self.now-timedelta(minutes=3))
        future=replace(self.candidate, observed_at=self.now+timedelta(seconds=1))
        self.assertEqual(self.entry(stale).reason, 'STALE_OR_FUTURE_DATA')
        self.assertEqual(self.entry(future).reason, 'STALE_OR_FUTURE_DATA')

    def test_daily_halt_or_live_order_never_possible(self):
        self.assertEqual(self.entry(account=replace(self.account, day_pnl=D(-2000))).reason,
                         'DAILY_LOSS_HALT')
        self.assertFalse(hasattr(self.entry(), 'submit_order'))

    def test_profit_target_uses_filled_basis_and_bid(self):
        plan=self.exit()
        self.assertEqual(plan.action,'PROPOSE_PAPER_SELL')
        self.assertEqual(plan.reason,'TAKE_PROFIT')
        self.assertEqual(plan.quantity,D(100))

    def test_target_not_reached_on_ask_only(self):
        quote=replace(self.quote, bid=D('51.49'), ask=D('51.80'))
        self.assertEqual(self.exit(quote=quote).reason,'TARGET_NOT_REACHED')

    def test_stop_priority(self):
        quote=replace(self.quote, bid=D(47), ask=D('47.10'))
        self.assertEqual(self.exit(quote=quote).reason,'PROTECTIVE_EXIT')

    def test_exit_pending_reconciliation_prevents_duplicate(self):
        p=replace(self.position, pending_exit_quantity=D(50))
        self.assertEqual(self.exit(position=p).reason,'PENDING_EXIT_RECONCILIATION')

    def test_exit_intent_key_reproducible(self):
        self.assertEqual(self.exit().intent_key,self.exit().intent_key)
        self.assertNotEqual(self.exit().intent_key,
                            self.exit(position=replace(self.position, position_id='paper-pos-2')).intent_key)

    def test_stale_or_extended_hours_exit_rejected(self):
        stale=replace(self.quote,observed_at=self.now-timedelta(minutes=3))
        extended=replace(self.quote,regular_session_verified=False)
        self.assertEqual(self.exit(quote=stale).reason,'STALE_OR_FUTURE_QUOTE')
        self.assertEqual(self.exit(quote=extended).reason,'UNVERIFIED_REGULAR_SESSION')

    def test_invalid_quote_or_position_rejected(self):
        self.assertEqual(self.exit(quote=replace(self.quote, regular_session_verified="yes")).reason,
                         "UNVERIFIED_REGULAR_SESSION")
        self.assertEqual(self.exit(position=replace(self.position, pending_exit_quantity=D("0.5"))).reason,
                         "INVALID_POSITION_STATE")
        self.assertEqual(self.exit(quote=replace(self.quote,bid=D('NaN'))).reason,'INVALID_QUOTE')
        self.assertEqual(self.exit(position=replace(self.position,filled_quantity=D('1.5'))).reason,
                         'INVALID_POSITION_STATE')

    def test_invalid_target_rejected(self):
        self.assertEqual(self.exit(take_profit_pct=D('NaN')).reason,'INVALID_TARGET')
        self.assertEqual(self.exit(take_profit_pct=D(0)).reason,'INVALID_TARGET')

    def test_no_auto_orders_while_market_unverified(self):
        self.assertEqual(self.exit(quote=replace(self.quote,regular_session_verified=False)).action,'HOLD')


if __name__ == '__main__':
    unittest.main()
