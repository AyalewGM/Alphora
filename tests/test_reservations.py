"""Offline SQLite cash-reservation, idempotency and synthetic-fill tests."""
import tempfile
import sqlite3
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from decimal import Decimal as D, localcontext
from pathlib import Path

from alphora.risk import BuyProposal, PaperAccount, RiskPolicy, INITIAL_EQUITY
from alphora.reservations import OfflineReservations


class OfflineReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.book = OfflineReservations(self.path, session_id="2026-11-02")

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def account(self, **changes):
        base = PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), "2026-11-02")
        return replace(base, **changes)

    def proposal(self, *, symbol="SPY", qty="100", price="50", stop="48", existing="0"):
        return BuyProposal(symbol, D(qty), D(price), D(stop), D(existing))

    def reserve(self, ident="a", proposal=None, **kw):
        return self.book.reserve_buy(ident, self.account(), proposal or self.proposal(),
                                     reconciled=True, **kw)

    def test_initial_zero_reservations_and_defaults(self):
        state = self.book.snapshot()
        self.assertEqual(state.cash, INITIAL_EQUITY)
        self.assertEqual(state.available_cash, INITIAL_EQUITY)
        self.assertEqual(state.pending, ())
        self.assertEqual(state.session_id, "2026-11-02")
        self.assertFalse(hasattr(self.book, "submit_order"))

    def test_persisted_policy_rejects_tiny_stricter_limit_at_low_precision(self):
        # Persisted default 0.5% risk identity must not collide with a
        # slightly tighter valid allowance when reopening at prec=3.
        initial = self.book.snapshot()
        self.book.close()
        try:
            with localcontext() as context:
                context.prec = 3
                tightened = RiskPolicy(
                    D("0.00499999999999999999999999"), D("0.10"))
                with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
                    OfflineReservations(
                        self.path, session_id="2026-11-02", policy=tightened)
        finally:
            self.book = OfflineReservations(
                self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), initial)

    def test_pending_reserves_cash_and_exact_id_is_idempotent(self):
        first = self.reserve(fee_budget=D("4"))
        self.assertEqual(first.reserved_cash, D("5004"))
        self.assertEqual(first.available_cash, D("94996"))
        self.assertEqual(self.reserve(fee_budget=D("4")), first)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_INTENT_ID"):
            self.reserve(proposal=self.proposal(qty="90"), fee_budget=D("4"))

    def test_multiple_pending_intents_share_instrument_cap(self):
        self.reserve("a", self.proposal(qty="120"))
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_ALLOCATION_EXCEEDED"):
            self.reserve("b", self.proposal(qty="100"))
        self.reserve("b", self.proposal(qty="80"))
        self.assertEqual(self.book.snapshot().reserved_cash, D("10000"))

    def test_cash_only_book_rejects_forged_equity_with_and_without_pending(self):
        # With no fills or SELLs, cash is the entire marked portfolio.
        # An algebraically consistent $120k caller mark cannot turn $100k
        # durable paper cash into a $12k instrument allocation allowance.
        # A working BUY reserves cash but does not create P&L.
        forged = self.account(equity=D("120000"), day_pnl=D("20000"))
        over_true_cap = self.proposal(qty="240", price="50", stop="49")
        before = self.book.snapshot()
        self.assertEqual(before.positions, ())
        self.assertEqual(before.cash, D("100000"))
        for intent in ("invented-equity",):
            with self.assertRaisesRegex(ValueError, "UNBACKED_ACCOUNT_EQUITY"):
                self.book.reserve_buy(intent, forged, over_true_cap,
                                      reconciled=True)
            self.assertEqual(self.book.snapshot(), before)

        self.reserve("existing-pending", self.proposal(qty="20", stop="49"))
        with_pending = self.book.snapshot()
        self.assertEqual(with_pending.cash, D("100000"))
        self.assertEqual(with_pending.positions, ())
        with self.assertRaisesRegex(ValueError, "UNBACKED_ACCOUNT_EQUITY"):
            self.book.reserve_buy("pending-forged-equity", forged,
                                  over_true_cap, reconciled=True)
        self.assertEqual(self.book.snapshot(), with_pending)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), with_pending)

    def test_exact_cap_after_fill_requires_authentic_mark_even_with_disjoint_pending(self):
        self.reserve("held", self.proposal(qty="120", stop="49"))
        self.book.record_hypothetical_fill("held", "filled-held", 120, D("50"))
        self.reserve("working", self.proposal(qty="60", stop="49"))
        before = self.book.snapshot()
        self.assertEqual(before.positions, (("SPY", 120, D("6000")),))
        self.assertEqual(before.reserved_cash, D("3000"))
        # $6k marked held + $3k working BUY + $1k new BUY is exactly
        # 10% of $100k. Old book formula double-counted pending to $13k.
        planned = BuyProposal("SPY", D("20"), D("50"), D("49"),
                              D("9000"), D("3000"))
        # Pending is not double-counted, but authentic holdings valuation
        # is still unavailable. Fail closed instead of admitting a new BUY.
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy("at-cap", self.account(), planned,
                                  reconciled=True)
        self.assertEqual(self.book.snapshot(), before)

    def test_stale_pending_witness_after_partial_fill_fails_closed_across_restart(self):
        self.reserve("held", self.proposal(qty="120", stop="49"))
        self.book.record_hypothetical_fill("held", "filled-held", 120, D("50"))
        self.reserve("working", self.proposal(qty="60", stop="49"))
        self.book.record_hypothetical_fill("working", "partial-working", 10, D("50"))
        old = self.book.snapshot()
        self.assertEqual(old.positions, (("SPY", 130, D("6500")),))
        self.assertEqual(old.pending, (("working", "SPY", 50, D("2500")),))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), old)
        stale = BuyProposal("SPY", D("20"), D("50"), D("49"),
                            D("9000"), D("3000"))
        with self.assertRaisesRegex(ValueError, "PENDING_RECEIPT_MISMATCH"):
            self.book.reserve_buy("stale-pending", self.account(), stale,
                                  reconciled=True)
        self.assertEqual(self.book.snapshot(), old)
        # Replanned held=6,500 and outstanding=2,500 fixes the pending
        # accounting witness, but cannot authenticate the current holding mark.
        updated = BuyProposal("SPY", D("20"), D("50"), D("49"),
                              D("9000"), D("2500"))
        # The corrected pending annotation is necessary but insufficient:
        # an independently verifiable holdings mark is still missing.
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy("fresh-pending", self.account(), updated,
                                  reconciled=True)
        self.assertEqual(self.book.snapshot(), old)

    def test_stale_direct_caller_cannot_understate_appreciated_holdings(self):
        self.reserve("held", self.proposal(qty="200", stop="49"))
        self.book.record_hypothetical_fill("held", "filled-held", 200, D("50"))
        before = self.book.snapshot()
        # Book's $10k acquisition cost is not the marked $16k holding.
        # Even when direct caller passes zero holdings, book must mark 200
        # shares at the proposed $80 price before admitting any more risk.
        stale = BuyProposal("SPY", D("1"), D("80"), D("79"), D("0"))
        marked = self.account(equity=D("106000"), day_pnl=D("6000"))
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy("stale-caller", marked, stale, reconciled=True)
        self.assertEqual(self.book.snapshot(), before)

    def test_lower_buy_limit_cannot_undervalue_appreciated_holding(self):
        # Single-symbol portfolio: mark is $80/share (200 * $80 = $16k),
        # but the new candidate's $50 BUY limit must not revalue holdings.
        self.reserve("held", self.proposal(qty="200", stop="49"))
        self.book.record_hypothetical_fill("held", "filled-held", 200, D("50"))
        before = self.book.snapshot()
        self.assertEqual(before.cash, D("90000"))
        mark = self.account(equity=D("106000"), day_pnl=D("6000"))
        stale = self.proposal(qty="1", price="50", stop="49", existing="0")
        # With $106k equity the instrument ceiling is $10,600. The old
        # code incorrectly substituted the new limit: 200*$50 + $50
        # equals $10,050 and therefore admitted this unsafe order.
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy("below-real-mark", mark, stale, reconciled=True)
        self.assertEqual(self.book.snapshot(), before)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)

    def test_stale_self_consistent_account_cannot_authorize_unmarked_existing_holding(self):
        # RED acceptance regression for #8/#80: the same-session caller can
        # provide equity/day P&L that agree algebraically with book cash while
        # being stale relative to the held security's current market mark.
        # The final SQLite reservation must demand a trusted, fresh,
        # session-bound per-symbol mark receipt or fail closed. A mere
        # reconciled=True caller assertion is not such evidence.
        self.reserve("held", self.proposal(qty="200", price="40", stop="39"))
        self.book.record_hypothetical_fill("held", "filled-held", 200, D("40"))
        before = self.book.snapshot()
        self.assertEqual(before.cash, D("92000"))
        self.assertEqual(before.positions, (("SPY", 200, D("8000")),))
        # Independent current mark is $80, so true SPY exposure is $16,000,
        # exceeding 10% of true $108,000 equity. The stale, internally
        # consistent $100,000 account instead makes the book see only $8,040
        # against a $10,000 cap when the new BUY limit is $40.
        stale_account = self.account(equity=D("100000"), day_pnl=D("0"))
        self.assertEqual(stale_account.cash, D("92000"))
        with self.assertRaisesRegex(
            ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"
        ):
            self.book.reserve_buy(
                "stale-consistent-mark", stale_account,
                self.proposal(qty="1", price="40", stop="39", existing="0"),
                reconciled=True,
            )
        self.assertEqual(self.book.snapshot(), before)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)

    def test_unverified_holdings_block_even_a_different_symbol_after_restart(self):
        # A stale portfolio equity can inflate a different symbol's 10%
        # allocation cap too. Holding SPY must not allow a QQQ reservation
        # without a fresh, session-bound full-portfolio valuation receipt.
        self.reserve("held-spy", self.proposal(qty="100", stop="49"))
        self.book.record_hypothetical_fill("held-spy", "fill-held-spy", 100, D("50"))
        before = self.book.snapshot()
        self.assertEqual(before.positions, (("SPY", 100, D("5000")),))
        stale = self.account(equity=D("100000"), day_pnl=D("0"))
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy(
                "new-qqq", stale,
                self.proposal(symbol="QQQ", qty="1", price="50", stop="49"),
                reconciled=True,
            )
        self.assertEqual(self.book.snapshot(), before)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)

    def test_multiple_held_symbols_require_per_symbol_mark_receipt(self):
        # Aggregate account equity alone cannot assign value between SPY
        # and QQQ. Do not infer either symbol's mark from a proposed limit.
        for sym in ("SPY", "QQQ"):
            self.reserve(sym, self.proposal(symbol=sym, qty="20", stop="49"))
            self.book.record_hypothetical_fill(sym, "fill-" + sym, 20, D("50"))
        before = self.book.snapshot()
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy(
                "ambiguous-mark", self.account(),
                self.proposal(qty="1", price="50", stop="49"),
                reconciled=True,
            )
        self.assertEqual(self.book.snapshot(), before)

    def test_mixed_pending_limit_prices_revalued_before_fee_cap(self):
        # Stage high/low limit orders first: a $100 quote with 196 already
        # held would correctly veto the high-limit order on its own. The
        # $50 seed fill may then coexist with both outstanding limits.
        self.reserve("high-limit", self.proposal(qty="1", price="100", stop="99"))
        self.reserve("low-limit", self.proposal(qty="1", price="20", stop="19"))
        self.reserve("held", self.proposal(qty="196", stop="49"))
        self.book.record_hypothetical_fill("held", "filled-held", 196, D("50"))
        before = self.book.snapshot()
        self.assertEqual(before.reserved_cash, D("120"))
        # At the new $50 quote: held $9800 + working max($100,$50)
        # and max($20,$50) = $9950, not $9920. A $50 new BUY
        # with $30 fee exceeds the $10k instrument cap by $30.
        proposal = BuyProposal("SPY", D("1"), D("50"), D("49"), D("9800"))
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.book.reserve_buy("unsafe", self.account(), proposal,
                                  fee_budget=D("30"), reconciled=True)
        self.assertEqual(self.book.snapshot(), before)

    def test_fee_budget_is_included_in_new_instrument_allocation(self):
        # Base $9,990 notional is below the $10,000 per-symbol cap, but
        # the reserved maximum fee is part of conservative exposure.
        near_cap = self.proposal(qty="100", price="99.90", stop="99.89")
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_ALLOCATION_EXCEEDED"):
            self.reserve("over-with-fees", near_cap, fee_budget=D("10.01"))
        self.assertEqual(self.book.snapshot().pending, ())
        at_cap = self.reserve("at-cap-with-fees", near_cap, fee_budget=D("10"))
        self.assertEqual(at_cap.reserved_cash, D("10000"))
        self.assertEqual(at_cap.available_cash, D("90000"))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), at_cap)

    def test_new_fee_budget_combines_with_existing_pending_fees(self):
        self.reserve("first", self.proposal(qty="100", stop="49"),
                     fee_budget=D("30"))
        # 5,030 earlier pending + 4,950 new notional + 21 fees = 10,001.
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_ALLOCATION_EXCEEDED"):
            self.reserve("second-over", self.proposal(qty="99", stop="49"),
                         fee_budget=D("21"))
        self.assertEqual(self.book.snapshot().reserved_cash, D("5030"))
        self.assertEqual(
            self.reserve("second-at-cap", self.proposal(qty="99", stop="49"),
                         fee_budget=D("20")).reserved_cash,
            D("10000"),
        )

    def test_subcent_fee_never_rounds_away_at_instrument_cap(self):
        # A prior pending $9,000 position plus a new $1,000 proposed
        # purchase reaches the exact 10% cap. Even a tiny positive fee
        # must not disappear from the existing-exposure Decimal sum.
        prior = self.reserve("first", self.proposal(qty="180", stop="49"))
        self.assertEqual(prior.reserved_cash, D("9000"))
        fee = D("0.0000000000000000000000001")
        with self.assertRaisesRegex(ValueError, "UNREPRESENTABLE_LEDGER_ARITHMETIC"):
            self.reserve("rounded-fee", self.proposal(qty="20", stop="49"),
                         fee_budget=fee)
        self.assertEqual(self.book.snapshot(), prior)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), prior)

    def test_subcent_synthetic_fill_fee_cannot_round_out_of_cash(self):
        # A one-share $50 fill with a tiny positive fee must never post
        # as exactly $50 of cash spent due to Decimal context precision.
        before = self.reserve("micro-fill", self.proposal(qty="1", stop="49"),
                              fee_budget=D("1"))
        tiny_fee = D("0.0000000000000000000000000001")
        with self.assertRaisesRegex(ValueError, "UNREPRESENTABLE_LEDGER_ARITHMETIC"):
            self.book.record_hypothetical_fill("micro-fill", "fill-1", 1, D("50"),
                                               fee=tiny_fee)
        self.assertEqual(self.book.snapshot(), before)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)

    def test_replay_rejects_corrupt_fill_with_rounded_away_fee(self):
        # A forged persisted fill may look cash-balanced under default
        # Decimal precision even though a positive fee was not deducted.
        self.reserve("micro-replay", self.proposal(qty="1", stop="49"),
                     fee_budget=D("1"))
        self.book.close()
        tiny_fee = "0.0000000000000000000000000001"
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO hypothetical_fills(id,intent_id,qty,price,fee) "
                "VALUES(?,?,?,?,?)",
                ("forged-fill", "micro-replay", 1, "50", tiny_fee),
            )
            db.execute(
                "UPDATE buy_intents SET filled=1, fees_paid=?, status='FILLED' "
                "WHERE id='micro-replay'", (tiny_fee,),
            )
            db.execute("UPDATE book_meta SET value='99950' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id="2026-11-02")

    def test_fee_budget_is_included_in_per_trade_planned_loss(self):
        # A fee cap is potential loss in addition to the price-to-stop
        # distance. With 100 shares at 50 and a stop at 48 the initial
        # $500 risk cap leaves at most $300 for synthetic fees.
        with self.assertRaisesRegex(ValueError, "RISK_VETO:TRADE_RISK_EXCEEDED"):
            self.reserve("fee-over", fee_budget=D("300.01"))
        self.assertEqual(self.book.snapshot().pending, ())
        state = self.reserve("fee-at-cap", fee_budget=D("300"))
        self.assertEqual(state.reserved_cash, D("5300"))
        self.assertEqual(state.available_cash, D("94700"))

    def test_daily_loss_and_planned_trade_risk_veto(self):
        with self.assertRaisesRegex(ValueError, "TRADE_RISK_EXCEEDED"):
            self.reserve("a", self.proposal(qty="100", stop="44"))
        account = self.account(equity=D("98000"), cash=D("98000"), day_pnl=D("-2000"))
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.reserve_buy("b", account, self.proposal(), reconciled=True)
        with self.assertRaisesRegex(ValueError, "DAILY_LOSS_HALT"):
            self.book.reserve_buy("b", self.account(equity=D("98000"),
                day_pnl=D("-2000")), self.proposal(), reconciled=True)

    def test_daily_loss_veto_persists_one_way_halt_across_restart(self):
        self.reserve("pending", self.proposal(symbol="SPY", qty="20"))
        before = self.book.snapshot()
        self.assertFalse(before.halted)
        loss_account = self.account(equity=D("98000"), day_pnl=D("-2000"))
        with self.assertRaisesRegex(ValueError, "RISK_VETO:DAILY_LOSS_HALT"):
            self.book.reserve_buy("rejected", loss_account,
                                  self.proposal(symbol="QQQ"),
                                  reconciled=True)
        state = self.book.snapshot()
        self.assertTrue(state.halted)
        self.assertEqual(state.pending, before.pending)
        self.assertEqual(state.cash, before.cash)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)
        # A later caller assertion of recovered equity must not un-halt.
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.reserve("after-recovery", self.proposal(symbol="QQQ"))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_fill("pending", "fill-after-stop",
                                               1, D("50"))
        self.assertEqual(self.book.cancel_remaining("pending").reserved_cash, D(0))

    def test_idempotent_intent_retry_cannot_hide_new_daily_loss(self):
        original = self.reserve("same-id", self.proposal(qty="20"))
        self.assertFalse(original.halted)
        loss = self.account(equity=D("98000"), day_pnl=D("-2000"))
        with self.assertRaisesRegex(ValueError, "DAILY_LOSS_HALT"):
            self.book.reserve_buy("same-id", loss, self.proposal(qty="20"),
                                  reconciled=True)
        halted = self.book.snapshot()
        self.assertTrue(halted.halted)
        self.assertEqual(halted.pending, original.pending)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.reserve("other-id", self.proposal(symbol="QQQ"))

    def test_explicit_loss_observation_does_not_need_new_order(self):
        self.reserve("pending", self.proposal(qty="20"))
        observed = self.book.observe_daily_loss(
            self.account(equity=D("98000"), day_pnl=D("-2000")),
            reconciled=True)
        self.assertTrue(observed.halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_fill("pending", "f1", 1, D("50"))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)

    def test_observer_rejects_unreconciled_and_inconsistent_marks(self):
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNRECONCILED_ACCOUNT"):
            self.book.observe_daily_loss(self.account())
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.observe_daily_loss(self.account(cash=D("99999")),
                                         reconciled=True)
        with self.assertRaisesRegex(ValueError, "INCONSISTENT_ACCOUNT_STATE"):
            self.book.observe_daily_loss(self.account(day_pnl=D("-50")),
                                         reconciled=True)
        self.assertFalse(self.book.snapshot().halted)
        # A caller-asserted manual upstream stop must propagate one-way.
        self.assertTrue(self.book.observe_daily_loss(
            self.account(halted=True), reconciled=True).halted)

    def test_daily_loss_observer_rejects_unrepresentable_marks_without_mutation(self):
        # A finite Decimal can overflow on subtraction. A just-above-limit
        # equity can instead round to an apparent $2,000 loss at default
        # precision; neither input may silently mutate the durable halt.
        before = self.book.snapshot()
        snapshots = (
            (D("1e1000000"), D("0")),
            (D("98000.00000000000000000000000001"),
             D("-1999.99999999999999999999999999")),
        )
        for equity, day_pnl in snapshots:
            with self.subTest(equity=str(equity)):
                unrepresentable = self.account(equity=equity, day_pnl=day_pnl)
                with self.assertRaisesRegex(ValueError, "UNREPRESENTABLE_ACCOUNT_MARK"):
                    self.book.observe_daily_loss(unrepresentable, reconciled=True)
                with self.assertRaisesRegex(ValueError, "UNREPRESENTABLE_ACCOUNT_MARK"):
                    self.book.reserve_buy("bad-mark", unrepresentable,
                                          self.proposal(qty="1"), reconciled=True)
                self.assertEqual(self.book.snapshot(), before)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)
        self.reserve("valid-after-mark-rejection", self.proposal(qty="1"))

    def test_non_loss_risk_veto_does_not_trigger_permanent_halt(self):
        with self.assertRaisesRegex(ValueError, "TRADE_RISK_EXCEEDED"):
            self.reserve("bad", self.proposal(qty="100", stop="44"))
        self.assertFalse(self.book.snapshot().halted)
        self.reserve("still-valid", self.proposal(qty="20"))

    def test_session_open_equity_cannot_be_forged_to_raise_risk_limits(self):
        # The virtual book starts a single fixed session at $100k. A caller
        # cannot raise or lower the opening-loss baseline to avoid the 2% stop.
        for opening_equity in (D("98000"), D("105000")):
            with self.subTest(opening_equity=opening_equity):
                forged = self.account(session_open_equity=opening_equity,
                                      equity=opening_equity, day_pnl=D("0"))
                with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
                    self.book.reserve_buy("forged", forged, self.proposal(),
                                          reconciled=True)
        self.assertEqual(self.book.snapshot().pending, ())

    def test_reopen_rejects_corrupt_cash_and_keeps_state_unchanged(self):
        self.reserve("a")
        before = self.book.snapshot()
        self.book.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value='99999' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id="2026-11-02")
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value=? WHERE key='cash'",
                         (str(before.cash),))
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)

    def test_failed_sqlite_fill_rolls_back_atomically_across_restart(self):
        self.reserve("a", fee_budget=D("2"))
        before = self.book.snapshot()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TRIGGER injected_fill_failure
                BEFORE INSERT ON hypothetical_fills BEGIN
                    SELECT RAISE(ABORT, 'injected failure');
                END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.record_hypothetical_fill("a", "partial-1", 20, D("49"),
                                                fee=D("1"))
        self.assertEqual(self.book.snapshot(), before)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM hypothetical_fills").fetchone()[0], 0)
            conn.execute("DROP TRIGGER injected_fill_failure")
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)
        after = self.book.record_hypothetical_fill("a", "partial-1",
                                                   20, D("49"), fee=D("1"))
        self.assertEqual(after.cash, D("99019"))

    def test_abrupt_child_exit_discards_uncommitted_sqlite_mutations(self):
        """A separate process dies with an open WAL write transaction."""
        self.reserve("abrupt")
        before = self.book.snapshot()
        script = """
import os
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1], timeout=5, isolation_level=None)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE book_meta SET value='1' WHERE key='halted'")
conn.execute("UPDATE buy_intents SET status='CANCELLED' WHERE id='abrupt'")
os._exit(23)  # No commit, rollback, context exit, or connection close.
"""
        child = subprocess.run([sys.executable, "-c", script, str(self.path)],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), before)
        self.assertFalse(self.book.snapshot().halted)

    def test_abrupt_child_exit_preserves_committed_synthetic_fill(self):
        """A process ends without cleanup immediately after a fill commit."""
        self.reserve("abrupt", fee_budget=D("2"))
        script = """
import os
import sys
from decimal import Decimal as D
from alphora.reservations import OfflineReservations
book = OfflineReservations(sys.argv[1], session_id='2026-11-02')
book.record_hypothetical_fill('abrupt', 'child-fill', 20, D('49'), fee=D('1'))
os._exit(24)  # No close or normal interpreter shutdown.
"""
        child = subprocess.run([sys.executable, "-c", script, str(self.path)],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 24, child.stderr)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        recovered = self.book.snapshot()
        self.assertEqual(recovered.cash, D("99019"))
        self.assertEqual(recovered.reserved_cash, D("4001"))
        self.assertEqual(recovered.positions, (("SPY", 20, D("981")),))
        self.assertEqual(self.book.record_hypothetical_fill(
            "abrupt", "child-fill", 20, D("49"), fee=D("1")), recovered)

    def test_cash_overcommit_across_pending_symbols(self):
        for i in range(10):
            self.reserve(f"i{i}", self.proposal(symbol=f"S{i}", qty="200"))
        self.assertEqual(self.book.snapshot().available_cash, D(0))
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_CASH"):
            self.reserve("extra", self.proposal(symbol="XYZ", qty="1"))

    def test_partial_fill_releases_price_improvement_and_keeps_remaining_locked(self):
        self.reserve("p", fee_budget=D("5"))
        state = self.book.record_hypothetical_fill("p", "f1", 40, D("49"), fee=D("2"))
        self.assertEqual(state.cash, D("98038"))
        self.assertEqual(state.reserved_cash, D("3003"))
        self.assertEqual(state.available_cash, D("95035"))
        self.assertEqual(state.positions, (("SPY", 40, D("1962")),))
        self.assertEqual(state.pending[0][2], 60)
        self.assertEqual(self.book.record_hypothetical_fill("p", "f1", 40, D("49"), fee=D("2")), state)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_FILL_ID"):
            self.book.record_hypothetical_fill("p", "f1", 40, D("49"), fee=D("3"))
        final = self.book.record_hypothetical_fill("p", "f2", 60, D("50"), fee=D("2"))
        self.assertEqual(final.cash, D("95036"))
        self.assertEqual(final.reserved_cash, D(0))
        self.assertEqual(final.positions, (("SPY", 100, D("4964")),))

    def test_fill_fees_enter_acquisition_cost_and_instrument_cap(self):
        # Actual synthetic buy fees reduce cash AND increase acquisition
        # cost basis; pending a trusted holdings mark, additional BUYs after
        # any fill must be vetoed rather than rely on historical cost.
        self.reserve("fees", self.proposal(qty="20", stop="48"),
                     fee_budget=D("100"))
        filled = self.book.record_hypothetical_fill(
            "fees", "fee-fill", 20, D("49"), fee=D("60"))
        self.assertEqual(filled.cash, D("98960"))
        self.assertEqual(filled.reserved_cash, D("0"))
        self.assertEqual(filled.positions, (("SPY", 20, D("1040")),))
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_MARK_RECEIPT_REQUIRED"):
            self.reserve("excess-exposure", self.proposal(qty="180", stop="49"))
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), filled)

    def test_no_overfill_gaps_or_unbudgeted_fees(self):
        self.reserve("a", fee_budget=D("1"))
        for qty, price, fee in ((101, D(50), D(0)), (1, D(51), D(0)),
                                (1, D(48), D(0)), (1, D(50), D(2))):
            with self.subTest(qty=qty, price=price, fee=fee):
                with self.assertRaisesRegex(ValueError, "FILL_EXCEEDS_LIMITS"):
                    self.book.record_hypothetical_fill("a", "new", qty, price, fee=fee)
        self.assertEqual(self.book.snapshot().cash, D("100000"))
        self.assertEqual(self.book.snapshot().reserved_cash, D("5001"))

    def test_cancel_releases_unfilled_reserve_but_retains_fills(self):
        self.reserve("a")
        self.book.record_hypothetical_fill("a", "f1", 20, D("50"))
        state = self.book.cancel_remaining("a")
        self.assertEqual(state.cash, D("99000"))
        self.assertEqual(state.reserved_cash, D(0))
        self.assertEqual(state.positions, (("SPY", 20, D("1000")),))
        self.assertEqual(self.book.cancel_remaining("a"), state)
        with self.assertRaisesRegex(ValueError, "NO_OPEN_INTENT"):
            self.book.record_hypothetical_fill("a", "f2", 1, D("50"))

    def test_cancelled_intent_retry_must_not_masquerade_as_reserved(self):
        proposal = self.proposal(qty="20")
        self.reserve("cancelled", proposal)
        cancelled = self.book.cancel_remaining("cancelled")
        self.assertEqual(cancelled.reserved_cash, D("0"))
        with self.assertRaisesRegex(ValueError, "CANCELLED_INTENT_ID"):
            self.reserve("cancelled", proposal)
        self.assertEqual(self.book.snapshot(), cancelled)
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        with self.assertRaisesRegex(ValueError, "CANCELLED_INTENT_ID"):
            self.reserve("cancelled", proposal)
        self.assertEqual(self.book.snapshot(), cancelled)
        # Restoring the desired virtual exposure requires a fresh intent ID.
        self.assertEqual(self.reserve("replacement", proposal).reserved_cash, D("1000"))

    def test_restart_persists_reservations_fills_and_halt(self):
        self.reserve("a", fee_budget=D("1"))
        self.book.record_hypothetical_fill("a", "f1", 10, D("49"))
        old = self.book.snapshot()
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertEqual(self.book.snapshot(), old)
        self.assertEqual(self.book.record_hypothetical_fill("a", "f1", 10, D("49")), old)
        self.book.halt()
        self.book.close()
        self.book = OfflineReservations(self.path, session_id="2026-11-02")
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.reserve("b", self.proposal(symbol="QQQ"))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.record_hypothetical_fill("a", "f2", 1, D("50"))
        self.assertEqual(self.book.cancel_remaining("a").reserved_cash, D(0))

    def test_reopen_mismatch_and_nondurable_paths_rejected(self):
        with self.assertRaisesRegex(ValueError, "BOOK_REOPEN_MISMATCH"):
            OfflineReservations(self.path, session_id="2026-11-03")
        for path in ("", " ", ":memory:", "file:paper?mode=memory", "file::memory:"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_BOOK"):
                    OfflineReservations(path, session_id="2026-11-02")

    def test_malformed_and_unreconciled_rejected(self):
        for ident in ("bad/id", "", "b"*129):
            with self.assertRaises(ValueError):
                self.reserve(ident)
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNRECONCILED_INTENT"):
            self.book.reserve_buy("a", self.account(), self.proposal(), reconciled=False)
        with self.assertRaisesRegex(ValueError, "UNRECONCILED_CASH_OR_SESSION"):
            self.book.reserve_buy("a", self.account(cash=D("99999")),
                                  self.proposal(), reconciled=True)
        for fill_qty in (0, 1.5, True, -1):
            with self.subTest(qty=fill_qty):
                with self.assertRaises(ValueError):
                    self.book.record_hypothetical_fill("a", "f", fill_qty, D("50"))

    def test_book_ledger_tampering_fails_closed(self):
        self.reserve("a")
        self.book.record_hypothetical_fill("a", "f1", 10, D("50"))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value='100000' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            self.book.snapshot()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE book_meta SET value='99500' WHERE key='cash'")
            conn.execute("DELETE FROM hypothetical_fills")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            self.book.snapshot()

    def test_corrupt_persisted_identities_and_decimals_fail_closed(self):
        """Malformed SQLite data must not be reinterpreted as valid holdings."""
        self.reserve("a")
        self.book.record_hypothetical_fill("a", "f1", 10, D("50"))
        before = self.book.snapshot()
        tampering = (
            ("UPDATE buy_intents SET symbol='spy' WHERE id='a'",
             "UPDATE buy_intents SET symbol='SPY' WHERE id='a'"),
            ("UPDATE hypothetical_fills SET id='bad/id' WHERE id='f1'",
             "UPDATE hypothetical_fills SET id='f1' WHERE id='bad/id'"),
            ("UPDATE buy_intents SET limit_price='bad' WHERE id='a'",
             "UPDATE buy_intents SET limit_price='50' WHERE id='a'"),
            ("UPDATE hypothetical_fills SET fee='bad' WHERE id='f1'",
             "UPDATE hypothetical_fills SET fee='0' WHERE id='f1'"),
        )
        for corrupt, restore in tampering:
            with self.subTest(sql=corrupt):
                with sqlite3.connect(self.path) as conn:
                    conn.execute(corrupt)
                with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
                    self.book.snapshot()
                # Reopen must also reject corrupted persisted state before
                # any new virtual intent can be accepted.
                self.book.close()
                with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
                    OfflineReservations(self.path, session_id="2026-11-02")
                with sqlite3.connect(self.path) as conn:
                    conn.execute(restore)
                self.book = OfflineReservations(self.path, session_id="2026-11-02")
                self.assertEqual(self.book.snapshot(), before)

    def test_missing_meta_with_existing_history_does_not_reseed_book(self):
        self.reserve("a")
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM book_meta")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id="2026-11-02")

    def test_initial_capital_cannot_be_overridden(self):
        for amount in (D("100001"), D("99999")):
            with self.subTest(value=amount):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_BOOK"):
                    OfflineReservations(self.path, session_id="2026-11-02",
                                        starting_cash=amount)

    def test_cross_connection_transaction_prevents_double_reserve(self):
        attempts = []
        def attempt(label):
            book = OfflineReservations(self.path, session_id="2026-11-02")
            try:
                book.reserve_buy(label, PaperAccount(D("100000"), D("100000"),
                    D("100000"), D("0"), "2026-11-02"),
                    self.proposal(symbol="SPY", qty="200"), reconciled=True)
                attempts.append("ok")
            except ValueError as exc:
                attempts.append(str(exc))
            finally:
                book.close()
        threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(attempts.count("ok"), 1)
        self.assertEqual(self.book.snapshot().reserved_cash, D("10000"))


if __name__ == "__main__":
    unittest.main()
