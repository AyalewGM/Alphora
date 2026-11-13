"""Offline forward-paper runner: single-session containment and safety."""
import tempfile
import unittest
from itertools import repeat
from datetime import date, datetime, timezone
from decimal import Decimal as D, localcontext
from pathlib import Path

from alphora.broker.paper import PaperBrokerAdapter
from alphora.orders import OrderLedger
from alphora.research.backtest import Bar
from alphora.reservations import OfflineReservations, PaperBookSnapshot
from alphora.runner import PaperSessionRunner, SessionDay, SessionSignal


def mt(day):
    return datetime(2026, 11, day, 21, 0, tzinfo=timezone.utc)


def bar(day, o, h, l, c, vol=1000):
    return Bar(date(2026, 11, day), D(o), D(h), D(l), D(c), vol)


def day(n, close, signals=None, extra=()):
    bars = {"SPY": bar(n, close, close, close, close)}
    bars.update(dict(extra))
    return SessionDay(f"2026-11-0{n}", mt(n), bars, signals or {})


WATCH = SessionSignal("WATCHLIST", "VERIFIED")
SKIP = SessionSignal("IGNORED", "VERIFIED")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book = OfflineReservations(root / "book.sqlite",
                                        session_id="2026-11-02")
        self.ledger = OrderLedger(root / "orders.sqlite",
                                  session_id="2026-11-02")
        self.venue = PaperBrokerAdapter()
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=self.venue,
            take_profit_pct=D("50"))

    def tearDown(self):
        self.book.close()
        self.ledger.close()
        self.tmp.cleanup()

    def test_one_session_entry_then_cross_session_rejected_without_mutation(self):
        report = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        d1, = report.days
        self.assertIn("ENTRY:SPY:ACKED", d1.actions)
        self.assertEqual(d1.positions, (("SPY", 200, D("10000")),))
        self.assertEqual(d1.cash, D("90000"))
        self.assertFalse(d1.halted)

        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_venue = self.venue.positions()
        # Session rollover has no durable opening-equity/mark reconciliation.
        # Reject even a risk-reducing-looking quote rather than silently
        # reusing the first day's fixed $100k daily loss baseline.
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            self.runner.run((day(3, "40", {"SPY": WATCH}),))
        self.assertEqual(self.book.snapshot(), before_book)
        self.assertEqual(self.ledger.orders(), before_orders)
        self.assertEqual(self.venue.positions(), before_venue)

    def test_future_day_at_first_call_and_batch_preflight_fail_before_mutations(self):
        before = self.book.snapshot()
        for supplied in (
            (day(3, "50", {"SPY": WATCH}),),
            (day(2, "50", {"SPY": WATCH}),
             day(3, "52", {"SPY": WATCH})),
        ):
            with self.subTest(n=len(supplied)):
                with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
                    self.runner.run(supplied)
                self.assertEqual(self.book.snapshot(), before)
                self.assertEqual(self.ledger.orders(), ())
                self.assertEqual(self.venue.positions(), ())
        # Rejection must not poison a subsequent valid single-session run.
        self.assertIn("ENTRY:SPY:ACKED",
                      self.runner.run((day(2, "50", {"SPY": WATCH}),)).days[0].actions)

    def test_ineligible_signal_is_skipped_not_dispatched(self):
        report = self.runner.run((day(2, "50", {"SPY": SKIP}),))
        self.assertIn("ENTRY_SKIP:SPY:SCANNER_NOT_ELIGIBLE",
                      report.days[0].actions)
        self.assertEqual(self.venue.open_orders(), ())
        self.assertEqual(self.book.snapshot().positions, ())

    def test_mark_limited_order_fills_same_day_and_nothing_lingers(self):
        report = self.runner.run((day(2, "60", {"SPY": WATCH}),))
        actions = report.days[0].actions
        self.assertIn("ENTRY:SPY:ACKED", actions)
        self.assertTrue(any(a.startswith("FILL:") for a in actions))
        self.assertEqual(self.venue.open_orders(), ())
        self.assertEqual(self.ledger.open_orders(), ())

    def test_recon_warn_does_not_block_but_fatal_state_surfaces(self):
        # A venue-side order unknown to the book makes recon FATAL: entries veto.
        from alphora.broker.base import DispatchRequest
        self.venue.dispatch(DispatchRequest("QQQ", "BUY", 1, D("10")),
                            client_order_id="z" * 64)
        report = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        d = report.days[0]
        # The unknown paper-venue order is expired during EOD cleanup,
        # so report the terminal certificate, not the earlier FATAL one.
        self.assertEqual(d.recon_status, "CLEAN")
        self.assertFalse(d.halted)
        self.assertTrue(any(a.startswith("ENTRY_VETO:SPY")
                            for a in d.actions), d.actions)

    def test_first_entry_uses_reconciliation_after_exit_sync(self):
        # No historical pre-exit certificate may be reused after settlement.
        import alphora.runner as runner_mod
        log = []
        real_reconcile, mgr = runner_mod.reconcile, self.runner._mgr
        real_sync, real_entry = mgr.sync, mgr.execute_entry

        def recording_reconcile(*args, **kwargs):
            result = real_reconcile(*args, **kwargs)
            log.append(("recon", result))
            return result

        def recording_sync(*args, **kwargs):
            log.append(("sync", None))
            return real_sync(*args, **kwargs)

        def recording_entry(*args, recon, **kwargs):
            log.append(("entry", recon))
            return real_entry(*args, recon=recon, **kwargs)

        runner_mod.reconcile = recording_reconcile
        mgr.sync, mgr.execute_entry = recording_sync, recording_entry
        try:
            report = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        finally:
            runner_mod.reconcile = real_reconcile
            mgr.sync, mgr.execute_entry = real_sync, real_entry
        self.assertIn("ENTRY:SPY:ACKED", report.days[0].actions)
        entry_at = next(i for i, (kind, _) in enumerate(log) if kind == "entry")
        before = log[:entry_at]
        last_recon = max(i for i, (kind, _) in enumerate(before) if kind == "recon")
        last_sync = max(i for i, (kind, _) in enumerate(before) if kind == "sync")
        self.assertGreater(last_recon, last_sync)
        self.assertIs(log[entry_at][1], before[last_recon][1])

    def _two_entry_session(self):
        return day(2, "50", {"SPY": WATCH, "QQQ": WATCH},
                   (("QQQ", bar(2, "40", "40", "40", "40")),))

    def test_each_buy_uses_a_distinct_fresh_reconciliation(self):
        import alphora.runner as runner_mod
        log = []
        real_reconcile, mgr = runner_mod.reconcile, self.runner._mgr
        real_entry = mgr.execute_entry

        def recording_reconcile(*args, **kwargs):
            result = real_reconcile(*args, **kwargs)
            log.append(("recon", result))
            return result

        def recording_entry(*args, recon, **kwargs):
            log.append(("entry", recon))
            return real_entry(*args, recon=recon, **kwargs)

        runner_mod.reconcile = recording_reconcile
        mgr.execute_entry = recording_entry
        try:
            report = self.runner.run((self._two_entry_session(),))
        finally:
            runner_mod.reconcile = real_reconcile
            mgr.execute_entry = real_entry
        actions = report.days[0].actions
        self.assertIn("ENTRY:QQQ:ACKED", actions)
        self.assertIn("ENTRY:SPY:ACKED", actions)
        entries = [i for i, (kind, _) in enumerate(log) if kind == "entry"]
        self.assertEqual(len(entries), 2)
        first, second = entries
        self.assertIsNot(log[first][1], log[second][1])
        self.assertTrue(any(kind == "recon" for kind, _ in log[first+1:second]))
        self.assertIs(log[second][1], log[second-1][1])

    def test_second_entry_fails_closed_on_new_venue_discrepancy(self):
        from alphora.broker.base import DispatchRequest
        mgr = self.runner._mgr
        real_entry, called = mgr.execute_entry, []

        def faulty_entry(*args, **kwargs):
            view = real_entry(*args, **kwargs)
            if not called:
                # Introduce an unknown venue order after the first BUY.
                self.venue.dispatch(DispatchRequest("QQQ", "BUY", 1, D("10")),
                                    client_order_id="z" * 64)
            called.append(view)
            return view

        mgr.execute_entry = faulty_entry
        try:
            report = self.runner.run((self._two_entry_session(),))
        finally:
            mgr.execute_entry = real_entry
        actions = report.days[0].actions
        self.assertIn("ENTRY:QQQ:ACKED", actions)
        self.assertTrue(any(a.startswith("ENTRY_VETO:SPY:STALE_OR_UNCLEAN")
                            for a in actions), actions)
        self.assertEqual(len(called), 1)
        self.assertEqual({o.symbol for o in self.ledger.orders()}, {"QQQ"})

    def test_terminal_reconciliation_detects_new_post_expiration_discrepancy(self):
        # Post-expiration rogue venue order must be exposed by final
        # reconciliation rather than reported as a stale CLEAN snapshot.
        from alphora.broker.base import DispatchRequest
        mgr = self.runner._mgr
        original_expire = mgr.expire_session

        def conflicting_expire(*, at):
            result = original_expire(at=at)
            self.venue.dispatch(DispatchRequest("QQQ", "BUY", 1, D("10")),
                                client_order_id="z" * 64)
            return result

        mgr.expire_session = conflicting_expire
        report = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", report.days[0].actions)
        self.assertEqual(report.days[0].recon_status, "FATAL")
        self.assertTrue(report.days[0].halted)
        self.assertTrue(self.book.snapshot().halted)

    def test_clean_terminal_reconciliation_allows_precise_fee_loss_observation(self):
        # The paper report must value settled positions and the order fee,
        # then independently attest its terminal portfolio state.
        self.venue = PaperBrokerAdapter(fee_per_order=D("1"))
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=self.venue,
            take_profit_pct=D("50"), fee_budget=D("1"))
        report = self.runner.run((day(2, "30", {"SPY": WATCH}),))
        self.assertEqual(report.days[0].recon_status, "CLEAN")
        self.assertEqual(report.final_equity, D("99999"))
        self.assertFalse(report.halted)

    def test_instrument_exposure_preserves_pending_reservation_cents(self):
        # A low ambient Decimal precision must not erase small pending
        # amounts when enforcing the per-instrument allocation limit.
        from dataclasses import replace
        snap = replace(self.book.snapshot(),
                       positions=(("SPY", 1, D("40")),),
                       pending=(("a", "SPY", 1, D("4990.04")),
                                ("b", "SPY", 1, D("4990.04")),
                                ("c", "QQQ", 1, D("10"))))
        with localcontext() as ctx:
            ctx.prec = 3
            held, pending = self.runner._instrument_exposure(
                snap, {"SPY": D("40.01")}, "SPY")
        self.assertEqual(held, D("40.01"))
        self.assertEqual(pending, D("9980.08"))
        impossible = replace(snap, pending=(
            ("a", "SPY", 1, D("1e80")),
            ("b", "SPY", 1, D("1"))))
        with self.assertRaisesRegex(ValueError,
                                    "INEXACT_PAPER_INSTRUMENT_EXPOSURE"):
            self.runner._instrument_exposure(
                impossible, {"SPY": D("40.01")}, "SPY")

    def test_appreciated_holdings_count_at_current_mark_not_purchase_cost(self):
        # A $10k holding at $50 appreciates to $16k at an $80 mark.
        # Original book cost is still $10k; no incremental BUY is allowed
        # under the owner's 10% current-equity symbol exposure ceiling.
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=self.venue,
            take_profit_pct=D("100"))
        first = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        self.assertEqual(self.book.snapshot().positions,
                         (("SPY", 200, D("10000")),))
        # Restart within SAME simulated session: multi-day rollover remains
        # unsupported until separately accepted under issue #42.
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=self.venue,
            take_profit_pct=D("100"))
        updated_day = SessionDay(
            "2026-11-02", datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc),
            {"SPY": bar(2, "80", "80", "80", "80")}, {"SPY": WATCH})
        second = self.runner.run((updated_day,))
        self.assertTrue(any(a.startswith("ENTRY_SKIP:SPY:")
                            for a in second.days[0].actions), second.days[0].actions)
        self.assertFalse(any(a.startswith("ENTRY:SPY:")
                             for a in second.days[0].actions), second.days[0].actions)
        self.assertEqual(self.book.snapshot().positions[0][1], 200)
        self.assertEqual(second.final_equity, D("106000"))

    def test_missing_held_symbol_mark_is_never_treated_as_break_even(self):
        self.runner.run((day(2, "50", {"SPY": WATCH}),))
        previous = self.book.snapshot()
        missing = SessionDay(
            "2026-11-02", mt(2),
            {"QQQ": bar(2, "25", "25", "25", "25")}, {})
        # A fresh in-memory runner must not silently value an existing
        # position at its cost basis when its actual mark is unavailable.
        fresh = PaperSessionRunner(book=self.book, ledger=self.ledger,
                                   adapter=self.venue)
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_OR_STALE_MARK"):
            fresh.run((missing,))
        self.assertEqual(self.book.snapshot(), previous)
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            fresh.run((day(3, "40"),))
        self.assertEqual(self.book.snapshot(), previous)

    def test_timestamp_must_match_us_eastern_session_and_unbounded_input_fails_closed(self):
        original = self.book.snapshot()
        original_orders = self.ledger.orders()
        # Nov 3 21:00 UTC is Nov 3 16:00 ET, not session Nov 2.
        # A past/future timestamp cannot masquerade as an executable
        # same-session mark just because the bar and book IDs agree.
        mismatched = SessionDay(
            "2026-11-02", mt(3), {"SPY": bar(2, "50", "50", "50", "50")},
            {"SPY": WATCH})
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_DAY"):
            self.runner.run((mismatched,))
        # An endless batch must stop after at most two elements; never
        # materialize the full iterator or process any hypothetical order.
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            self.runner.run(repeat(day(2, "50", {"SPY": WATCH})))
        self.assertEqual(self.book.snapshot(), original)
        self.assertEqual(self.ledger.orders(), original_orders)
        self.assertEqual(self.venue.positions(), ())
        # UTC calendar rollover is not automatically an ET day rollover.
        valid_late_utc = SessionDay(
            "2026-11-02", datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc),
            {"SPY": bar(2, "50", "50", "50", "50")}, {})
        self.assertEqual(self.runner.run((valid_late_utc,)).days[0].day,
                         "2026-11-02")

    def test_empty_session_batch_fails_closed_without_invented_equity(self):
        self.runner.run((day(2, "50", {"SPY": WATCH}),))
        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_venue = self.venue.positions()
        with self.assertRaisesRegex(ValueError, "MISSING_SESSION_MARK"):
            self.runner.run(())
        self.assertEqual(self.book.snapshot(), before_book)
        self.assertEqual(self.ledger.orders(), before_orders)
        self.assertEqual(self.venue.positions(), before_venue)

    def test_paper_account_mark_is_independent_of_caller_decimal_precision(self):
        # Regression: 200 * $40.01 + $90k cash is $98,002, NOT $98,000.
        # A caller precision of 3 previously rounded this above-threshold
        # mark down to the exact 2% daily-loss halt boundary.
        snap = PaperBookSnapshot(
            cash=D("90000"), reserved_cash=D(0),
            available_cash=D("90000"),
            positions=(("SPY", 200, D("10000")),),
            pending=(), halted=False, session_id="2026-11-02")
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as ctx:
                ctx.prec = precision
                account = self.runner._account(snap, {"SPY": D("40.01")})
                self.assertEqual(account.equity, D("98002"))
                self.assertEqual(account.day_pnl, D("-1998"))
                self.assertGreater(account.equity, D("98000"))
        # Extreme finite values that cannot be summed exactly inside the
        # bounded accounting context must fail closed, never misstate P&L.
        with self.assertRaisesRegex(ValueError, "INEXACT_PAPER_ACCOUNT"):
            self.runner._account(snap, {"SPY": D("1e60")})

    def test_report_and_loss_observation_use_post_fill_fee_equity(self):
        # The first pre-entry mark is $100,000. A same-day synthetic buy
        # costing a $1 fee makes the *settled* equity $99,999; the final
        # report must not silently return the stale pre-fill $100,000.
        venue = PaperBrokerAdapter(fee_per_order=D("1"))
        runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=venue,
            fee_budget=D("1"), take_profit_pct=D("50"))
        report = runner.run((day(2, "60", {"SPY": WATCH}),))
        final = self.book.snapshot()
        self.assertTrue(any(a.startswith("ENTRY:SPY:")
                            for a in report.days[0].actions))
        self.assertEqual(final.positions[0][0], "SPY")
        expected_equity = final.cash + final.positions[0][1] * D("60")
        self.assertEqual(expected_equity, D("99999"))
        self.assertEqual(report.days[0].equity, expected_equity)
        self.assertEqual(report.final_equity, expected_equity)
        self.assertFalse(report.days[0].halted)

    def test_inexact_paper_mark_rejected_before_account_or_venue_mutation(self):
        # The offline paper venue rounds each supplied mark to cents while
        # runner equity was formerly computed at the raw mark. Refuse a
        # fractional-cent mark rather than diverging on held valuation,
        # loss-limit enforcement and reported final equity.
        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_venue = self.venue.positions()
        for price, reason in (
            ("50.004", "INEXACT_PAPER_MARK"),
            ("50.005", "INEXACT_PAPER_MARK"),
            ("NaN", "INVALID_MARKS"),
            ("Infinity", "INVALID_MARKS"),
        ):
            with self.subTest(price=price):
                with self.assertRaisesRegex(ValueError, reason):
                    self.runner.run((day(2, price, {"SPY": WATCH}),))
                self.assertEqual(self.book.snapshot(), before_book)
                self.assertEqual(self.ledger.orders(), before_orders)
                self.assertEqual(self.venue.positions(), before_venue)
        # A rejected first mark must not poison a later valid same-day call.
        accepted = self.runner.run((day(2, "50.00", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", accepted.days[0].actions)
        self.assertEqual(accepted.final_equity, D("100000"))

    def test_malformed_session_signals_rejected_before_any_paper_mutation(self):
        # Previously None caused AttributeError after advance/sync and a
        # zero explicit stop silently fell back to the implicit 4% stop.
        # Neither should consume the session or create a paper order.
        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_venue = self.venue.positions()
        invalid_signals = (
            None,
            {"SPY": None},
            {None: WATCH},
            {"SPY": SessionSignal("WATCHLIST", "VERIFIED", D("0"))},
            {"SPY": SessionSignal("WATCHLIST", "VERIFIED", D("NaN"))},
            {"SPY": SessionSignal("WATCHLIST", "VERIFIED", D("50"))},
            {"SPY": SessionSignal("WATCHLIST", "VERIFIED", D("51"))},
            {"SPY": SessionSignal("WATCHLIST", "VERIFIED", "45")},
        )
        for bad in invalid_signals:
            with self.subTest(signals=repr(bad)):
                supplied = SessionDay("2026-11-02", mt(2),
                                      {"SPY": bar(2, "50", "50", "50", "50")},
                                      bad)
                with self.assertRaisesRegex(ValueError, "INVALID_SESSION_SIGNAL"):
                    self.runner.run((supplied,))
                self.assertEqual(self.book.snapshot(), before_book)
                self.assertEqual(self.ledger.orders(), before_orders)
                self.assertEqual(self.venue.positions(), before_venue)
                self.assertIsNone(self.runner._last_day)
        # Valid input may still be run after any rejected malformed signals.
        accepted = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", accepted.days[0].actions)

    def test_invalid_symbol_and_signal_fields_preflight_before_paper_mutation(self):
        # A well-formed SPY order must not slip through because another
        # caller-provided signal is malformed. The whole input is rejected
        # before advancing paper marks or consuming this session.
        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_positions = self.venue.positions()
        before_working = self.venue.open_orders()
        invalid = (
            {"SPY": WATCH, "ZZ invalid": WATCH},
            {"SPY": WATCH, "ZZZZZZZZZZZ": WATCH},
            {"SPY": WATCH, "": WATCH},
            {"SPY": SessionSignal(None, "VERIFIED")},
            {"SPY": SessionSignal("WATCHLIST", None)},
            {"SPY": SessionSignal(["WATCHLIST"], "VERIFIED")},
            {"SPY": SessionSignal("WATCHLIST", ["VERIFIED"])},
        )
        for signals in invalid:
            with self.subTest(signals=repr(signals)):
                supplied = SessionDay("2026-11-02", mt(2),
                                      {"SPY": bar(2, "50", "50", "50", "50")},
                                      signals)
                with self.assertRaisesRegex(ValueError, "INVALID_SESSION_SIGNAL"):
                    self.runner.run((supplied,))
                self.assertEqual(self.book.snapshot(), before_book)
                self.assertEqual(self.ledger.orders(), before_orders)
                self.assertEqual(self.venue.positions(), before_positions)
                self.assertEqual(self.venue.open_orders(), before_working)
                self.assertIsNone(self.runner._last_day)

        # The venue rejects an invalid mark symbol itself, but only after
        # the runner formerly marked the session as consumed. Fail earlier.
        bad_bars = {"SPY": bar(2, "50", "50", "50", "50"),
                    "ZZ invalid": bar(2, "50", "50", "50", "50")}
        with self.assertRaisesRegex(ValueError, "INVALID_MARKS"):
            self.runner.run((SessionDay("2026-11-02", mt(2),
                                        bad_bars, {"SPY": WATCH}),))
        self.assertEqual(self.book.snapshot(), before_book)
        self.assertEqual(self.ledger.orders(), before_orders)
        self.assertEqual(self.venue.positions(), before_positions)
        self.assertIsNone(self.runner._last_day)
        accepted = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", accepted.days[0].actions)

    def test_missing_session_marks_fail_closed_before_mutation(self):
        # An empty day with no prices previously returned the original
        # $100,000 as though a current session mark had been supplied.
        # Malformed bars containers likewise must fail with a stable error
        # before the venue, book or ledger can advance.
        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_venue = self.venue.positions()
        before_working = self.venue.open_orders()
        for bars, error in (({}, "MISSING_SESSION_MARK"),
                            (None, "INVALID_MARKS"),
                            ([], "INVALID_MARKS")):
            with self.subTest(bars=repr(bars)):
                supplied = SessionDay("2026-11-02", mt(2), bars, {})
                with self.assertRaisesRegex(ValueError, error):
                    self.runner.run((supplied,))
                self.assertEqual(self.book.snapshot(), before_book)
                self.assertEqual(self.ledger.orders(), before_orders)
                self.assertEqual(self.venue.positions(), before_venue)
                self.assertEqual(self.venue.open_orders(), before_working)
                self.assertIsNone(self.runner._last_day)
        accepted = self.runner.run((day(2, "50", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", accepted.days[0].actions)

    def test_cent_executable_default_stop_limits_low_price_risk(self):
        # Mark $0.02, default 4% stop would be a non-executable $0.0192.
        # Nearest *lower* executable tick is $0.01. Risk must be sized
        # against a real $0.01 per-share loss, not a fictitious $0.0008.
        report = self.runner.run((day(2, "0.02", {"SPY": WATCH}),))
        buys = [o for o in self.ledger.orders() if o.side == "BUY"]
        self.assertEqual(len(buys), 1)
        order, = buys
        self.assertEqual(order.stop_price, D("0.01"))
        self.assertEqual(order.qty, 50000)
        self.assertEqual((D("0.02") - order.stop_price) * order.qty,
                         D("500"))  # approved 0.5% of $100,000
        self.assertEqual(self.book.snapshot().positions,
                         (("SPY", 50000, D("1000")),))
        self.assertEqual(report.final_equity, D("100000"))

    def test_fractional_cent_or_unrepresentable_stop_rejects_before_mutation(self):
        # $0.01 cannot support a positive stop below the entry price when
        # both marks and fills use cents. Caller-supplied subcent stops
        # cannot quietly become a much larger real tick-loss exposure.
        before_book = self.book.snapshot()
        before_orders = self.ledger.orders()
        before_venue = self.venue.positions()
        cases = (
            ("0.01", WATCH, "UNREPRESENTABLE_PAPER_STOP"),
            ("0.02", SessionSignal("WATCHLIST", "VERIFIED", D("0.019")),
             "INEXACT_PAPER_STOP"),
            ("50.00", SessionSignal("WATCHLIST", "VERIFIED", D("49.999")),
             "INEXACT_PAPER_STOP"),
        )
        for price, signal, reason in cases:
            with self.subTest(price=price, reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    self.runner.run((day(2, price, {"SPY": signal}),))
                self.assertEqual(self.book.snapshot(), before_book)
                self.assertEqual(self.ledger.orders(), before_orders)
                self.assertEqual(self.venue.positions(), before_venue)
                self.assertEqual(self.venue.open_orders(), ())
                self.assertIsNone(self.runner._last_day)
        accepted = self.runner.run((day(2, "0.02", {"SPY": WATCH}),))
        self.assertIn("ENTRY:SPY:ACKED", accepted.days[0].actions)

    def test_default_stop_floors_to_cent_for_nonpenny_mark(self):
        report = self.runner.run((day(2, "40.01", {"SPY": WATCH}),))
        buys = [o for o in self.ledger.orders() if o.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].stop_price, D("38.40"))
        self.assertEqual(buys[0].qty, 249)  # $10,000 instrument cap
        self.assertLessEqual(
            buys[0].qty * (D("40.01") - buys[0].stop_price), D("500"))
        self.assertEqual(report.final_equity, D("100000"))

    def test_day_and_config_validation(self):
        for kw in ({"mark_price": "mid"}, {"take_profit_pct": D("-1")},
                   {"stop_fraction": D("nan")}, {"fee_budget": D("-1")}):
            with self.subTest(kw=kw):
                with self.assertRaisesRegex(ValueError, "INVALID"):
                    PaperSessionRunner(book=self.book, ledger=self.ledger,
                                       adapter=self.venue, **kw)
        naive = SessionDay("2026-11-02", datetime(2026, 11, 2), {}, {})
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_DAY"):
            self.runner.run((naive,))
        again = SessionDay("2026-11-02", mt(2),
                           {"SPY": bar(2, "1", "1", "1", "1")}, {})
        self.runner.run((again,))
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_DAY"):
            self.runner.run((again,))  # non-increasing day rejected


if __name__ == "__main__":
    unittest.main()
