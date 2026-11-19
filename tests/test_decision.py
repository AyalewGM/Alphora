import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.risk import PaperAccount
from alphora.decision import (EntryCandidate, FilledPosition, ExitQuote,
                              propose_entry, propose_exit)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
        self.account = PaperAccount(D('100000'), D('100000'), D('100000'), D('0'), '2026-11-02')
        self.candidate = EntryCandidate('SPY', D('50'), D('48'), self.now,
                                        'WATCHLIST', 'VERIFIED', D('50'), 'SPY', True)
        self.position = FilledPosition('paper-pos-1', 'SPY', D('100'), D('50'), D('48'), 1)
        self.quote = ExitQuote('SPY', D('51.50'), D('51.52'), self.now, True)

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

    def test_entry_plan_retains_disjoint_pending_witness_for_book_recheck(self):
        candidate = replace(self.candidate,
                            existing_instrument_value=D("8000"),
                            pending_instrument_value=D("1000"))
        plan = self.entry(candidate)
        self.assertTrue(plan.permitted)
        self.assertEqual(plan.proposal.existing_instrument_value, D("9000"))
        self.assertEqual(plan.proposal.pending_included_value, D("1000"))
        self.assertEqual(plan.proposal.quantity, D("20"))

    def test_inexact_cash_reservation_fails_closed(self):
        # Default 28-digit Decimal arithmetic would round a tiny but real
        # reservation away from the $10,000 cash available for a full-cap buy.
        account = replace(self.account, cash=D("10000"))
        result = self.entry(account=account,
                            reserved_cash=D("0.0000000000000000000000000001"))
        self.assertFalse(result.permitted)
        self.assertEqual(result.reason, "INVALID_ACCOUNT_OR_ORDER")

    def test_inexact_pending_exposure_fails_closed(self):
        # This pending exposure would round away when added to $1,000,
        # otherwise permitting 180 shares at $50 and exceeding the cap.
        candidate = replace(self.candidate,
                            existing_instrument_value=D("1000"),
                            pending_instrument_value=D("0.0000000000000000000000000001"))
        result = self.entry(candidate)
        self.assertFalse(result.permitted)
        self.assertEqual(result.reason, "INVALID_ACCOUNT_OR_ORDER")
        # Exact, normal-size reservations must still size deterministically.
        self.assertEqual(self.entry(reserved_cash=D("97000")).proposal.quantity,
                         D(60))

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

    def test_fall_back_quote_freshness_rejects_stale_and_future_entry(self):
        # Synthetic DST fold chronology only: this Sunday is not a market-open assertion.
        eastern = ZoneInfo("America/New_York")
        stale_mark = datetime(2026, 11, 1, 1, 58, tzinfo=eastern, fold=0)
        later = datetime(2026, 11, 1, 1, 59, tzinfo=eastern, fold=1)
        # Wall clock says 1 minute old; absolute age is 61 minutes.
        self.assertEqual(
            self.entry(replace(self.candidate, observed_at=stale_mark),
                       as_of=later).reason,
            "STALE_OR_FUTURE_DATA",
        )
        future_mark = datetime(2026, 11, 1, 1, 0, tzinfo=eastern, fold=1)
        earlier = datetime(2026, 11, 1, 1, 1, tzinfo=eastern, fold=0)
        # Wall clock says 1 minute old; the mark is actually 59 minutes ahead.
        self.assertEqual(
            self.entry(replace(self.candidate, observed_at=future_mark),
                       as_of=earlier).reason,
            "STALE_OR_FUTURE_DATA",
        )

    def test_fall_back_quote_freshness_rejects_stale_and_future_exit(self):
        eastern = ZoneInfo("America/New_York")
        stale_mark = datetime(2026, 11, 1, 1, 58, tzinfo=eastern, fold=0)
        later = datetime(2026, 11, 1, 1, 59, tzinfo=eastern, fold=1)
        future_mark = datetime(2026, 11, 1, 1, 0, tzinfo=eastern, fold=1)
        earlier = datetime(2026, 11, 1, 1, 1, tzinfo=eastern, fold=0)
        self.assertEqual(
            self.exit(quote=replace(self.quote, observed_at=stale_mark),
                      as_of=later).reason,
            "STALE_OR_FUTURE_QUOTE",
        )
        self.assertEqual(
            self.exit(quote=replace(self.quote, observed_at=future_mark),
                      as_of=earlier).reason,
            "STALE_OR_FUTURE_QUOTE",
        )

    def test_fall_back_quote_freshness_accepts_true_one_minute_age(self):
        eastern = ZoneInfo("America/New_York")
        observed = datetime(2026, 11, 1, 1, 59, tzinfo=eastern, fold=0)
        as_of = datetime(2026, 11, 1, 1, 0, tzinfo=eastern, fold=1)
        self.assertTrue(
            self.entry(replace(self.candidate, observed_at=observed),
                       as_of=as_of).permitted,
        )
        self.assertEqual(
            self.exit(quote=replace(self.quote, observed_at=observed),
                      as_of=as_of).reason,
            "TAKE_PROFIT",
        )

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

    def test_unrepresentable_positive_target_cannot_trigger_break_even_sale(self):
        # With default 28-digit Decimal precision a tiny positive target
        # rounds 1 + pct/100 back to 1. A $50 bid must never trigger it.
        quote = replace(self.quote, bid=D("50"), ask=D("50.01"))
        result = self.exit(quote=quote, take_profit_pct=D("1e-30"))
        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.reason, "INVALID_POSITION_OR_QUOTE")
        # The ordinary exact 3% target remains valid.
        self.assertEqual(self.exit().reason, "TAKE_PROFIT")

    def test_target_not_reached_on_ask_only(self):
        quote=replace(self.quote, bid=D('51.49'), ask=D('51.80'))
        self.assertEqual(self.exit(quote=quote).reason,'TARGET_NOT_REACHED')

    def test_stop_priority(self):
        quote=replace(self.quote, bid=D(47), ask=D('47.10'))
        self.assertEqual(self.exit(quote=quote).reason,'PROTECTIVE_EXIT')

    def test_exit_pending_reconciliation_prevents_duplicate(self):
        p=replace(self.position, pending_exit_quantity=D(50))
        self.assertEqual(self.exit(position=p).reason,'PENDING_EXIT_RECONCILIATION')

    def test_exit_plan_is_bound_to_evaluated_symbol(self):
        plan = self.exit(quote=replace(self.quote, bid=self.position.protective_stop_price))
        self.assertEqual(plan.action, "PROPOSE_PAPER_SELL")
        self.assertEqual(plan.symbol, self.position.symbol)

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


    def test_planner_rejects_untyped_snapshots(self):
        # The existing offline planner guards must veto all four top-level
        # boundaries, not only missing accounts/positions. These are not
        # broker orders and must not produce executable intent identifiers.
        entry_args = dict(as_of=self.now, expected_session_id='2026-11-02',
                          reserved_cash=D(0), reconciled=True)
        exit_args = dict(as_of=self.now, take_profit_pct=D(3))
        for malformed in (None, {}, object()):
            with self.subTest(boundary='entry-account', kind=type(malformed).__name__):
                plan = propose_entry(malformed, self.candidate, **entry_args)
                self.assertFalse(plan.permitted)
                self.assertEqual(plan.reason, 'INVALID_ACCOUNT_OR_ORDER')
                self.assertIsNone(plan.proposal)
                self.assertIsNone(plan.intent_key)
            with self.subTest(boundary='entry-candidate', kind=type(malformed).__name__):
                plan = propose_entry(self.account, malformed, **entry_args)
                self.assertFalse(plan.permitted)
                self.assertEqual(plan.reason, 'INVALID_ACCOUNT_OR_ORDER')
                self.assertIsNone(plan.proposal)
                self.assertIsNone(plan.intent_key)
            with self.subTest(boundary='exit-position', kind=type(malformed).__name__):
                plan = propose_exit(malformed, self.quote, **exit_args)
                self.assertEqual(plan.action, 'HOLD')
                self.assertEqual(plan.reason, 'INVALID_POSITION_OR_QUOTE')
                self.assertIsNone(plan.intent_key)
            with self.subTest(boundary='exit-quote', kind=type(malformed).__name__):
                plan = propose_exit(self.position, malformed, **exit_args)
                self.assertEqual(plan.action, 'HOLD')
                self.assertEqual(plan.reason, 'INVALID_POSITION_OR_QUOTE')
                self.assertIsNone(plan.intent_key)

    def test_entry_extreme_decimal_fails_closed_without_exception(self):
        huge = D('1e1000000')
        snapshot = replace(self.account, equity=huge, session_open_equity=huge, cash=huge)
        self.assertEqual(self.entry(account=snapshot).reason, 'INVALID_ACCOUNT_OR_ORDER')
        candidate = replace(self.candidate, existing_instrument_value=huge,
                            pending_instrument_value=huge)
        self.assertEqual(self.entry(candidate).reason, 'INVALID_ACCOUNT_OR_ORDER')
        self.assertEqual(propose_entry(None, self.candidate, as_of=self.now,
                      expected_session_id='2026-11-02', reserved_cash=D(0),
                      reconciled=True).reason, 'INVALID_ACCOUNT_OR_ORDER')

    def test_exit_extreme_decimal_fails_closed_without_exception(self):
        position = replace(self.position, average_fill_price=D('1e1000000'))
        quote = replace(self.quote, bid=D('100'), ask=D('100.01'))
        result = self.exit(position=position, quote=quote)
        self.assertEqual(result.action, 'HOLD')
        self.assertEqual(result.reason, 'INVALID_POSITION_OR_QUOTE')
        self.assertEqual(propose_exit(None, quote, as_of=self.now,
                        take_profit_pct=D(3)).reason, 'INVALID_POSITION_OR_QUOTE')

    def test_daily_loss_from_equity_cannot_be_bypassed(self):
        account=replace(self.account,equity=D(95000),cash=D(95000),day_pnl=D(0))
        self.assertEqual(self.entry(account=account).reason,'DAILY_LOSS_HALT')

    def test_account_inconsistency_and_excess_cash_rejected(self):
        account=replace(self.account,equity=D(99500),cash=D(99500),day_pnl=D(0))
        self.assertEqual(self.entry(account=account).reason,'INCONSISTENT_ACCOUNT_STATE')
        self.assertEqual(self.entry(account=replace(self.account,cash=D(1000000))).reason,
                         'INCONSISTENT_ACCOUNT_STATE')

    def test_verified_ask_used_for_entry_risk(self):
        plan=self.entry(replace(self.candidate,ask=D(51)))
        self.assertTrue(plan.permitted)
        self.assertEqual(plan.proposal.price,D(51))
        self.assertEqual(plan.proposal.quantity,D(166))

    def test_entry_quote_symbol_and_session_enforced(self):
        self.assertEqual(self.entry(replace(self.candidate,quote_symbol='QQQ')).reason,
                         'QUOTE_SYMBOL_MISMATCH')
        self.assertEqual(self.entry(replace(self.candidate,regular_session_verified=False)).reason,
                         'UNVERIFIED_REGULAR_SESSION')
        self.assertEqual(self.entry(replace(self.candidate,regular_session_verified='true')).reason,
                         'UNVERIFIED_REGULAR_SESSION')

    def test_entry_intent_key_reproducible(self):
        self.assertEqual(self.entry().intent_key,self.entry().intent_key)
        self.assertIsNotNone(self.entry().intent_key)
        different=self.entry(replace(self.candidate,ask=D('50.01'))).intent_key
        self.assertNotEqual(self.entry().intent_key,different)

    def test_entry_exit_reject_symbols_that_persistent_book_cannot_store(self):
        for symbol in ("---", "...", "1SPY", "A" * 11):
            with self.subTest(symbol=symbol):
                candidate = replace(self.candidate, symbol=symbol, quote_symbol=symbol)
                self.assertEqual(self.entry(candidate).reason, "INVALID_SYMBOL")
                position = replace(self.position, symbol=symbol)
                quote = replace(self.quote, symbol=symbol)
                self.assertEqual(self.exit(position=position, quote=quote).reason,
                                 "INVALID_SYMBOL")
        self.assertEqual(self.exit(position=replace(self.position, symbol="BRK.B"),
                                   quote=replace(self.quote, symbol="BRK.B")).reason,
                         "TAKE_PROFIT")

    def test_exit_quote_bound_to_symbol(self):
        self.assertEqual(self.exit(quote=replace(self.quote,symbol='QQQ')).reason,
                         'QUOTE_SYMBOL_MISMATCH')

    def test_partial_fill_followed_by_stop_creates_distinct_key(self):
        target=self.exit()
        remaining=replace(self.position,filled_quantity=D(40),reconciliation_version=2)
        quote=replace(self.quote,bid=D(47),ask=D('47.01'))
        stop=self.exit(position=remaining,quote=quote)
        self.assertEqual(stop.quantity,D(40))
        self.assertEqual(stop.reason,'PROTECTIVE_EXIT')
        self.assertNotEqual(target.intent_key,stop.intent_key)
        self.assertEqual(stop.intent_key,self.exit(position=remaining,quote=quote).intent_key)

    def test_partial_pending_exit_does_not_block_remaining_stop(self):
        p=replace(self.position,pending_exit_quantity=D(50))
        quote=replace(self.quote,bid=D(40),ask=D('40.01'))
        plan=self.exit(position=p,quote=quote)
        self.assertEqual(plan.action,'PROPOSE_PAPER_SELL')
        self.assertEqual(plan.reason,'PROTECTIVE_EXIT')
        self.assertEqual(plan.quantity,D(50))
        self.assertEqual(self.exit(position=p).action,'BLOCKED_REQUIRES_RECONCILIATION')

    def test_break_even_and_raised_stop_still_protect(self):
        quote=replace(self.quote,bid=D(30),ask=D('30.01'))
        for stop in (D(50),D(55)):
            p=replace(self.position,protective_stop_price=stop)
            self.assertEqual(self.exit(position=p,quote=quote).reason,'PROTECTIVE_EXIT')

    def test_reconciliation_version_is_required(self):
        p=replace(self.position,reconciliation_version=0)
        self.assertEqual(self.exit(position=p).reason,'INVALID_POSITION_STATE')

    def test_freshness_policy_cannot_be_set_to_a_year(self):
        self.assertEqual(self.entry(max_age_seconds=10**9).reason,'INVALID_FRESHNESS_POLICY')
        self.assertEqual(self.exit(max_age_seconds=10**9).reason,'INVALID_FRESHNESS_POLICY')

if __name__ == '__main__':
    unittest.main()
