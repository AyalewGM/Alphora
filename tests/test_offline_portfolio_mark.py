"""Synthetic-only portfolio mark correctness and fail-closed risk veto tests."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.offline_portfolio_mark import (
    HeldPosition, PortfolioMarkInput, SourceQuote, MarkResult,
    mark_reconciled_paper_portfolio,
)
from alphora.risk import DAILY_LOSS_FRACTION, INITIAL_EQUITY, validate_buy, BuyProposal

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 11, 9, 10, 0, 0, tzinfo=ET)


def book(*, cash=D("0"), positions=(HeldPosition("SPY", 1000),), **updates):
    return PortfolioMarkInput(
        session_id="2026-11-09", session_open_equity=INITIAL_EQUITY,
        cash=cash, positions=positions, reconciled=True,
        pending_state_clear=True, halted=False, **updates
    )


def quote(*, bid=D("100"), ask=D("100.10"), observed_at=AS_OF,
          symbol="SPY", source_verified=True):
    return SourceQuote(symbol, bid, ask, observed_at, source_verified)


def marked(snapshot=None, quotes=None, **changes):
    options = {"as_of": AS_OF, "session_verified": True}
    options.update(changes)
    return mark_reconciled_paper_portfolio(
        snapshot if snapshot is not None else book(),
        quotes if quotes is not None else (quote(),),
        **options,
    )


class OfflinePortfolioMarkTests(unittest.TestCase):
    def test_start_equity_and_default_owner_loss_policy(self):
        self.assertEqual(INITIAL_EQUITY, D("100000"))
        self.assertEqual(DAILY_LOSS_FRACTION, D("0.02"))
        result = marked()
        self.assertTrue(result.valid)
        self.assertEqual(result.account.equity, INITIAL_EQUITY)
        self.assertEqual(result.account.cash, D(0))
        self.assertEqual(result.account.day_pnl, D(0))
        self.assertFalse(result.halt_required)
        self.assertFalse(result.account.halted)

    def test_bid_not_ask_and_exact_daily_halt_boundary(self):
        # An ask that would show an unrealized gain cannot mask a bid loss.
        loss = marked(quotes=(quote(bid=D("98"), ask=D("98.50")),))
        self.assertTrue(loss.valid)
        self.assertEqual(loss.account.equity, D("98000"))
        self.assertEqual(loss.account.day_pnl, D("-2000"))
        self.assertTrue(loss.halt_required)
        self.assertEqual(loss.reason, "DAILY_LOSS_HALT_REQUIRED")
        above = marked(quotes=(quote(bid=D("98.01"), ask=D("98.50")),))
        self.assertTrue(above.valid)
        self.assertFalse(above.halt_required)
        below = marked(quotes=(quote(bid=D("97.99"), ask=D("98.10")),))
        self.assertTrue(below.halt_required)

    def test_realized_loss_with_no_positions_still_halts(self):
        flat = marked(snapshot=book(cash=D("97000"), positions=()), quotes=())
        self.assertTrue(flat.valid)
        self.assertEqual(flat.account.equity, D("97000"))
        self.assertTrue(flat.halt_required)
        good = marked(snapshot=book(cash=D("100000"), positions=()), quotes=())
        self.assertTrue(good.valid)
        self.assertFalse(good.halt_required)

    def test_persisted_previous_halt_never_cleared_by_rally(self):
        snapshot = replace(book(), halted=True)
        result = marked(snapshot=snapshot, quotes=(quote(bid=D("101"), ask=D("101.1")),))
        self.assertTrue(result.valid)
        self.assertTrue(result.halt_required)
        self.assertTrue(result.account.halted)
        self.assertEqual(result.account.day_pnl, D("1000"))

    def test_missing_extra_duplicate_or_mismatched_quote_veto(self):
        first = HeldPosition("SPY", 10)
        second = HeldPosition("QQQ", 10)
        two = book(positions=(first, second))
        for snap, quotes, code in (
            (two, (quote(),), "QUOTE_SET_MISMATCH"),
            (book(), (quote(), quote()), "QUOTE_SET_MISMATCH"),
            (book(), (quote(symbol="QQQ"),), "QUOTE_SET_MISMATCH"),
            (two, (quote(), quote()), "QUOTE_SET_MISMATCH"),
            (book(positions=()), (quote(),), "QUOTE_SET_MISMATCH"),
        ):
            with self.subTest(reason=code, snap=snap, quotes=quotes):
                result = marked(snapshot=snap, quotes=quotes)
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, code)
                self.assertIsNone(result.account)

    def test_invalid_positions_and_unreconciled_state_fail_closed(self):
        examples = (
            replace(book(), positions=(HeldPosition("SPY", 10), HeldPosition("SPY", 20))),
            replace(book(), positions=(HeldPosition("QQQ", True),)),
            replace(book(), positions=(HeldPosition("../secrets", 10),)),
            replace(book(), positions=(HeldPosition("QQQ", 0),)),
            replace(book(), positions=(HeldPosition("QQQ", 1000000001),)),
        )
        for snap in examples:
            with self.subTest(positions=snap.positions):
                result = marked(snapshot=snap)
                self.assertEqual(result.reason, "INVALID_POSITION_SET")
                self.assertIsNone(result.account)
        for snap in (
            replace(book(), reconciled=False),
            replace(book(), pending_state_clear=False),
            replace(book(), reconciled=1),
        ):
            self.assertEqual(marked(snapshot=snap).reason, "RECONCILIATION_REQUIRED")

    def test_stale_future_crossed_untrusted_invalid_marks(self):
        q = (
            quote(observed_at=AS_OF-timedelta(seconds=6)),
            quote(observed_at=AS_OF+timedelta(microseconds=1)),
            quote(bid=D("50"), ask=D("51")),
            quote(bid=D("51"), ask=D("50")),
            quote(bid=D("0")),
            quote(bid=D("NaN")),
            quote(bid=D("Infinity")),
            quote(source_verified=False),
            quote(observed_at=datetime(2026, 11, 9, 10)),
        )
        for item in q:
            with self.subTest(quote=item):
                result = marked(quotes=(item,))
                self.assertEqual(result.reason, "UNVERIFIED_OR_STALE_QUOTE")
                self.assertIsNone(result.account)
        self.assertTrue(marked(quotes=(
            quote(observed_at=AS_OF-timedelta(seconds=5)),)).valid)

    def test_spread_exact_boundary_and_inexact_near_miss(self):
        allowed = marked(quotes=(quote(bid=D("199"), ask=D("201")),))
        self.assertTrue(allowed.valid)  # exactly 1 percent of midpoint
        beyond = marked(quotes=(quote(
            bid=D("199"), ask=D("201." + "0"*52 + "1")),))
        self.assertEqual(beyond.reason, "UNVERIFIED_OR_STALE_QUOTE")
        self.assertIsNone(beyond.account)

    def test_bad_session_nonpaper_external_cash_or_unsupported_input(self):
        for value in ("live", "paper-connected", "broker"):
            self.assertEqual(marked(mode=value).reason, "REAL_TRADING_DISABLED")
        self.assertEqual(marked(session_verified=False).reason, "UNVERIFIED_SESSION")
        self.assertEqual(marked(snapshot=replace(
            book(), session_id="2026-11-08")).reason, "SESSION_MISMATCH")
        self.assertEqual(mark_reconciled_paper_portfolio(
            book(), (quote(),), as_of=datetime(2026,11,9,10),
            session_verified=True).reason, "SESSION_MISMATCH")
        self.assertEqual(marked(snapshot=replace(
            book(), external_cash_flow=D("1"))).reason,
            "INVALID_OR_EXTERNAL_CASH_FLOW")
        self.assertEqual(marked(snapshot=replace(
            book(), cash=D("-1"))).reason, "INVALID_OR_EXTERNAL_CASH_FLOW")
        self.assertEqual(marked(snapshot=replace(
            book(), halted=1)).reason, "INVALID_OR_EXTERNAL_CASH_FLOW")

    def test_utc_absolute_age_and_dst(self):
        # Offset-aware UTC and Eastern instants are equivalent.
        utc_quote = quote(observed_at=AS_OF.astimezone(timezone.utc))
        self.assertTrue(marked(quotes=(utc_quote,)).valid)
        summer = datetime(2026, 7, 6, 10, tzinfo=ET)
        summer_book = replace(book(), session_id=summer.date().isoformat())
        summer_q = quote(observed_at=summer.astimezone(timezone.utc))
        self.assertTrue(mark_reconciled_paper_portfolio(
            summer_book, (summer_q,), as_of=summer,
            session_verified=True).valid)
        self.assertNotEqual(summer.utcoffset(), AS_OF.utcoffset())

    def test_extreme_money_precision_veto_and_no_broker_surface(self):
        snap = replace(book(), cash=D("1e60"),
                       session_open_equity=D("1e60"),
                       positions=(HeldPosition("SPY", 1),))
        result = marked(snapshot=snap, quotes=(quote(bid=D("1"), ask=D("1.001")),))
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "UNREPRESENTABLE_VALUATION")
        self.assertIsNone(result.account)
        import alphora.offline_portfolio_mark as module
        for name in ("submit_order", "execute_trade", "connect_alpaca",
                     "poll_market_data", "record_paper_fill", "start_scheduler"):
            self.assertFalse(hasattr(module, name))

    def test_derived_halt_account_cannot_authorize_buy(self):
        loss = marked(quotes=(quote(bid=D("98"), ask=D("98.1")),))
        proposal = BuyProposal("SPY", D("1"), D("98"), D("95"))
        verdict = validate_buy(
            loss.account, proposal, expected_session_id="2026-11-09")
        self.assertFalse(verdict.permitted)
        self.assertEqual(verdict.reason, "DAILY_LOSS_HALT")


if __name__ == "__main__":
    unittest.main()
