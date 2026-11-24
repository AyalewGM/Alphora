"""End-to-end paper order-manager flow, idempotency and crash-recovery tests."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

from alphora.broker.base import DispatchAck, DispatchRequest
from alphora.broker.manager import PaperOrderManager
from alphora.broker.paper import PaperBrokerAdapter
from alphora.decision import EntryCandidate, EntryPlan, ExitPlan, propose_entry
from alphora.orders import OrderLedger
from alphora.reconcile import reconcile
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
KEY = "a" * 64


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book = OfflineReservations(root / "book.sqlite",
                                        session_id="2026-11-02")
        self.ledger = OrderLedger(root / "orders.sqlite",
                                  session_id="2026-11-02")
        self.venue = PaperBrokerAdapter()
        self.mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                     adapter=self.venue)

    def tearDown(self):
        self.book.close()
        self.ledger.close()
        self.tmp.cleanup()

    def reopen(self):
        self.book.close()
        self.ledger.close()
        root = Path(self.tmp.name)
        self.book = OfflineReservations(root / "book.sqlite",
                                        session_id="2026-11-02")
        self.ledger = OrderLedger(root / "orders.sqlite",
                                  session_id="2026-11-02")
        self.mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                     adapter=self.venue)

    def account(self, **changes):
        base = PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), "2026-11-02")
        return replace(base, **changes)

    def entry(self, key=KEY, qty="100", price="50", stop="48", symbol="SPY"):
        proposal = BuyProposal(symbol, D(qty), D(price), D(stop))
        return EntryPlan(True, "PAPER_BUY_ALLOWED", proposal, key)

    def recon(self, at_time=None):
        return reconcile(self.book, self.ledger, self.venue,
                         at=at_time or at())

    def test_manager_requires_paper_components_and_matching_session(self):
        class FakeAdapter:
            mode = "live"
        with self.assertRaisesRegex(ValueError, "REAL_TRADING_DISABLED"):
            PaperOrderManager(book=self.book, ledger=self.ledger,
                              adapter=FakeAdapter())
        with self.assertRaisesRegex(ValueError, "SESSION_MISMATCH"):
            other = OrderLedger(Path(self.tmp.name) / "o2.sqlite",
                                session_id="2026-11-03")
            try:
                PaperOrderManager(book=self.book, ledger=other,
                                  adapter=self.venue)
            finally:
                other.close()

    def test_entry_end_to_end_book_ledger_venue_consistent(self):
        view = self.mgr.execute_entry(self.entry(), self.account(),
                                      recon=self.recon(), now=at(),
                                      reconciled=True)
        self.assertEqual(view.status, "ACKED")
        self.assertEqual(view.broker_order_id, "paper-00000001")
        self.assertEqual(self.book.snapshot().reserved_cash, D("5000"))
        events = self.mgr.advance({"SPY": D("49")}, at=at(1))
        self.assertEqual(len(events), 1)
        done = self.ledger.get(KEY)
        self.assertEqual((done.status, done.filled), ("FILLED", 100))
        book = self.book.snapshot()
        self.assertEqual(book.cash, D("95100"))
        self.assertEqual(book.reserved_cash, D(0))
        self.assertEqual(book.positions, (("SPY", 100, D("4900")),))
        snap = self.venue.account_snapshot()
        self.assertEqual(snap.cash, D("95100"))
        venue_pos = self.venue.positions()
        self.assertEqual(venue_pos[0].qty, 100)
        self.assertEqual(venue_pos[0].avg_price, D("49"))

    def test_entry_retry_is_idempotent_through_dispatch(self):
        plan = self.entry()
        first = self.mgr.execute_entry(plan, self.account(),
                                       recon=self.recon(), now=at(),
                                       reconciled=True)
        again = self.mgr.execute_entry(plan, self.account(),
                                       recon=self.recon(at(1)), now=at(1),
                                       reconciled=True)
        self.assertEqual(first, again)
        self.assertEqual(len(self.venue.open_orders()), 1)
        self.assertEqual(len(self.ledger.orders()), 1)
        self.assertEqual(self.book.snapshot().reserved_cash, D("5000"))

    def test_legacy_semantic_entry_collision_never_reaches_paper_venue(self):
        original = self.entry(qty="20", price="50", stop="49")
        first = self.mgr.execute_entry(original, self.account(),
                                       recon=self.recon(), now=at(),
                                       reconciled=True)
        self.assertEqual(first.status, "ACKED")
        before_book = self.book.snapshot()
        before_ledger = self.ledger.orders()
        before_venue = self.venue.open_orders()
        # Same order terms, a different ID (as generated from alternate
        # timestamp/Decimal spellings by the historical decision hash).
        alias = self.entry("b" * 64, qty="20", price="50.00", stop="49.00")
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_LEGACY_ENTRY_INTENT"):
            self.mgr.execute_entry(alias, self.account(),
                                   recon=self.recon(at(1)), now=at(1),
                                   reconciled=True)
        self.assertEqual(self.book.snapshot(), before_book)
        self.assertEqual(self.ledger.orders(), before_ledger)
        self.assertEqual(self.venue.open_orders(), before_venue)
        self.reopen()
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_LEGACY_ENTRY_INTENT"):
            self.mgr.execute_entry(alias, self.account(),
                                   recon=self.recon(at(2)), now=at(2),
                                   reconciled=True)
        self.assertEqual(self.book.snapshot(), before_book)
        self.assertEqual(self.ledger.orders(), before_ledger)
        self.assertEqual(self.venue.open_orders(), before_venue)

    def test_generated_quote_aliases_do_not_duplicate_paper_dispatch(self):
        # The historical entry hash uses raw timestamp/Decimal spellings.
        # Plan all three encodings against the SAME pre-reservation snapshot:
        # both independently fit the 10% cap, so an allocation veto alone
        # cannot demonstrate cross-encoding replay safety.
        source = EntryCandidate(
            "SPY", D("50"), D("40"), at(), "WATCHLIST", "VERIFIED",
            D("50"), "SPY", True)
        variants = (
            source,
            replace(source, observed_at=at().astimezone(
                ZoneInfo("America/New_York"))),
            replace(source, price=D("50.00"), ask=D("50.00"),
                    stop_price=D("40.00")),
        )
        def make_plan(candidate):
            plan = propose_entry(
                self.account(), candidate, as_of=at(),
                expected_session_id="2026-11-02", reserved_cash=D(0),
                reconciled=True)
            self.assertTrue(plan.permitted)
            self.assertEqual(plan.proposal.quantity, D("50"))
            return plan
        original, *aliases = (make_plan(candidate) for candidate in variants)
        self.assertTrue(all(p.proposal == original.proposal for p in aliases))
        first = self.mgr.execute_entry(
            original, self.account(), recon=self.recon(), now=at(),
            reconciled=True)
        self.assertEqual(first.status, "ACKED")
        before_book = self.book.snapshot()
        before_ledger = self.ledger.orders()
        before_venue = self.venue.open_orders()
        self.assertEqual(before_book.reserved_cash, D("2500"))
        # A future canonical key may match the original, in which case
        # normal exact-ID replay is appropriate. With legacy distinct IDs,
        # the book must quarantine the ambiguity before venue dispatch.
        for restart in (False, True):
            if restart:
                self.reopen()
            for alias in aliases:
                if alias.intent_key == original.intent_key:
                    self.assertEqual(
                        self.mgr.execute_entry(
                            alias, self.account(), recon=self.recon(at(2)),
                            now=at(2), reconciled=True),
                        first)
                else:
                    with self.assertRaisesRegex(
                            ValueError, "AMBIGUOUS_LEGACY_ENTRY_INTENT"):
                        self.mgr.execute_entry(
                            alias, self.account(), recon=self.recon(at(2)),
                            now=at(2), reconciled=True)
                self.assertEqual(self.book.snapshot(), before_book)
                self.assertEqual(self.ledger.orders(), before_ledger)
                self.assertEqual(self.venue.open_orders(), before_venue)

    def test_entry_venue_reject_releases_reservation(self):
        class RejectingVenue(PaperBrokerAdapter):
            def dispatch(self, req, *, client_order_id):
                return DispatchAck(client_order_id, None, False,
                                   "INSUFFICIENT_CASH")
        venue = RejectingVenue()
        mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                adapter=venue)
        plan = self.entry(qty="200")
        view = mgr.execute_entry(plan, self.account(), recon=self.recon(),
                                 now=at(), reconciled=True)
        self.assertEqual((view.status, view.terminal_reason),
                         ("REJECTED", "INSUFFICIENT_CASH"))
        book = self.book.snapshot()
        self.assertEqual(book.reserved_cash, D(0))
        self.assertEqual(book.pending, ())
        # A terminal rejected intent cannot be silently resubmitted.
        with self.assertRaisesRegex(ValueError, "CANCELLED_INTENT_ID"):
            mgr.execute_entry(plan, self.account(), recon=self.recon(at(1)),
                              now=at(1), reconciled=True)

    def test_entry_risk_veto_never_creates_order(self):
        bad = self.entry(stop="44")  # (50-44)*100 = 600 > $500 cap
        with self.assertRaisesRegex(ValueError, "TRADE_RISK_EXCEEDED"):
            self.mgr.execute_entry(bad, self.account(), recon=self.recon(),
                                   now=at(), reconciled=True)
        self.assertIsNone(self.ledger.get(KEY))
        self.assertEqual(self.venue.open_orders(), ())
        self.assertEqual(self.book.snapshot().pending, ())

    def test_direct_entry_rejects_subcent_ticks_before_book_or_ledger_writes(self):
        # The runner enforces cent stops, but a direct PaperOrderManager caller
        # previously reserved cash and journaled an intent before the venue
        # rejected a fractional-cent limit, or accepted a subcent stop.
        initial = self.book.snapshot()
        cases = (
            (self.entry(price="50.001", stop="48"), "INEXACT_PAPER_PRICE"),
            (self.entry(price="50", stop="49.999"), "INEXACT_PAPER_STOP"),
            (self.entry(price="0.02", stop="0.019", qty="100"),
             "INEXACT_PAPER_STOP"),
        )
        for plan, error in cases:
            with self.subTest(error=error, price=str(plan.proposal.price),
                              stop=str(plan.proposal.stop_price)):
                with self.assertRaisesRegex(ValueError, error):
                    self.mgr.execute_entry(plan, self.account(),
                                           recon=self.recon(), now=at(),
                                           reconciled=True)
                self.assertEqual(self.book.snapshot(), initial)
                self.assertEqual(self.ledger.orders(), ())
                self.assertEqual(self.venue.open_orders(), ())
                self.assertEqual(self.venue.positions(), ())
        # Rejected requests cannot poison the next valid intent.
        result = self.mgr.execute_entry(self.entry(), self.account(),
                                        recon=self.recon(), now=at(),
                                        reconciled=True)
        self.assertEqual(result.status, "ACKED")

    def test_entry_unreconciled_or_unpermitted_fails(self):
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNPERMITTED_PLAN"):
            self.mgr.execute_entry(self.entry(), self.account(),
                                   recon=self.recon(), now=at())
        denied = EntryPlan(False, "STALE_OR_FUTURE_DATA", None, KEY)
        with self.assertRaisesRegex(ValueError, "INVALID_OR_UNPERMITTED_PLAN"):
            self.mgr.execute_entry(denied, self.account(), recon=self.recon(),
                                   now=at(), reconciled=True)
        self.assertEqual(self.ledger.orders(), ())

    def test_warn_recon_blocks_new_entry(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        # Venue fills but the manager has not synced: UNSETTLED_FILLS (WARN).
        self.venue.advance({"SPY": D("50")}, at=at(1))
        warn = self.recon(at(1))
        self.assertEqual(warn.status, "WARN")
        self.assertIn("UNSETTLED_FILLS", {d.kind for d in warn.diffs})
        before = (self.ledger.orders(), self.venue.open_orders())
        with self.assertRaisesRegex(ValueError,
                                    "STALE_OR_UNCLEAN_RECONCILIATION"):
            self.mgr.execute_entry(self.entry("d" * 64, symbol="QQQ"),
                                   self.account(), recon=warn, now=at(1),
                                   reconciled=True)
        self.assertEqual((self.ledger.orders(), self.venue.open_orders()),
                         before)

    def test_own_undelivered_intent_resumes_but_blocks_other_entries(self):
        plan = self.entry()
        # Crash after the hold and ledger record, before dispatch.
        self.book.reserve_buy(KEY, self.account(), plan.proposal,
                              reconciled=True)
        self.ledger.create(KEY, symbol="SPY", side="BUY", qty=100,
                           limit_price=D("50"), stop_price=D("48"), now=at())
        warn = self.recon(at(1))
        self.assertEqual(warn.status, "WARN")
        with self.assertRaisesRegex(ValueError,
                                    "STALE_OR_UNCLEAN_RECONCILIATION"):
            self.mgr.execute_entry(self.entry("d" * 64, symbol="QQQ"),
                                   self.account(), recon=warn, now=at(1),
                                   reconciled=True)
        view = self.mgr.execute_entry(plan, self.account(), recon=warn,
                                      now=at(1), reconciled=True)
        self.assertEqual(view.status, "ACKED")
        self.assertEqual(self.recon(at(2)).status, "CLEAN")

    def test_entry_requires_fresh_nonfatal_recon(self):
        stale = reconcile(self.book, self.ledger, self.venue,
                          at=at() - timedelta(seconds=200))
        with self.assertRaisesRegex(ValueError, "STALE_OR_UNCLEAN_RECONCILIATION"):
            self.mgr.execute_entry(self.entry(), self.account(), recon=stale,
                                   now=at(), reconciled=True)
        # A fresh CLEAN certificate permits dispatch.
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        # A fatal certificate (here: injected venue order unknown to ledger)
        # blocks any further dispatch.
        self.venue.dispatch(DispatchRequest("QQQ", "BUY", 1, D("10")),
                            client_order_id="z" * 64)
        fatal = self.recon(at(1))
        self.assertEqual(fatal.status, "FATAL")
        with self.assertRaisesRegex(ValueError, "STALE_OR_UNCLEAN_RECONCILIATION"):
            self.mgr.execute_entry(self.entry("d" * 64), self.account(),
                                   recon=fatal, now=at(1), reconciled=True)

    def test_fall_back_reconciliation_requires_true_utc_freshness(self):
        # Sunday DST folds are synthetic timestamp controls, not sessions.
        eastern = ZoneInfo("America/New_York")
        stale = datetime(2026, 11, 1, 1, 58, tzinfo=eastern, fold=0)
        later = datetime(2026, 11, 1, 1, 59, tzinfo=eastern, fold=1)
        future = datetime(2026, 11, 1, 1, 0, tzinfo=eastern, fold=1)
        earlier = datetime(2026, 11, 1, 1, 1, tzinfo=eastern, fold=0)
        for observed, decision in ((stale, later), (future, earlier)):
            with self.subTest(observed=observed, decision=decision):
                certificate = replace(self.recon(), checked_at=observed)
                with self.assertRaisesRegex(
                        ValueError, "STALE_OR_UNCLEAN_RECONCILIATION"):
                    self.mgr.execute_entry(self.entry(), self.account(),
                                           recon=certificate, now=decision,
                                           reconciled=True)
        self.assertEqual(self.ledger.orders(), ())
        self.assertEqual(self.book.snapshot().pending, ())
        # Actual one-minute age across the fold is acceptable to this
        # chronology guard (separate session/business rules still apply).
        valid_mark = datetime(2026, 11, 1, 1, 59, tzinfo=eastern, fold=0)
        valid_now = datetime(2026, 11, 1, 1, 0, tzinfo=eastern, fold=1)
        self.mgr._require_clean_recon(
            replace(self.recon(), checked_at=valid_mark), valid_now, 120)

    def test_entry_blocked_when_book_halted(self):
        self.book.halt()
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.mgr.execute_entry(self.entry(), self.account(),
                                   recon=self.recon(), now=at(),
                                   reconciled=True)
        self.assertIsNone(self.ledger.get(KEY))

    def test_lost_ack_recovers_via_client_key(self):
        # Simulate a crash after dispatch: venue has the order, ledger is SENT.
        proposal = BuyProposal("SPY", D("100"), D("50"), D("48"))
        self.book.reserve_buy(KEY, self.account(), proposal, reconciled=True)
        self.ledger.create(KEY, symbol="SPY", side="BUY", qty=100,
                           limit_price=D("50"), stop_price=D("48"), now=at())
        self.ledger.mark_sent(KEY, now=at(1))
        self.venue.dispatch(DispatchRequest("SPY", "BUY", 100, D("50")),
                            client_order_id=KEY)
        actions = self.mgr.sync(now=at(2))
        self.assertIn(f"ACKED:{KEY}", actions)
        view = self.ledger.get(KEY)
        self.assertEqual((view.status, view.broker_order_id),
                         ("ACKED", "paper-00000001"))

    def test_never_sent_order_confirmed_absent_and_released(self):
        # Crash between mark_sent and dispatch: venue never saw it.
        proposal = BuyProposal("SPY", D("100"), D("50"), D("48"))
        self.book.reserve_buy(KEY, self.account(), proposal, reconciled=True)
        self.ledger.create(KEY, symbol="SPY", side="BUY", qty=100,
                           limit_price=D("50"), stop_price=D("48"), now=at())
        self.ledger.mark_sent(KEY, now=at(1))
        actions = self.mgr.sync(now=at(2))
        self.assertIn(f"ABSENT:{KEY}", actions)
        self.assertEqual(self.ledger.get(KEY).status, "CANCELLED")
        self.assertEqual(self.book.snapshot().reserved_cash, D(0))

    def test_terminal_fill_recovers_after_book_write_failure(self):
        """An ACKed final fill must settle exactly once after a book fault."""
        from unittest.mock import patch

        view = self.mgr.execute_entry(self.entry(), self.account(),
                                      recon=self.recon(), now=at(),
                                      reconciled=True)
        self.assertEqual(view.status, "ACKED")
        before = self.book.snapshot()
        # Inject the failure at the second persistence boundary only.
        # Keep the simulated venue alive, as in the existing reopen fixture.
        with patch.object(OfflineReservations, "record_hypothetical_fill",
                          side_effect=RuntimeError("INJECTED_BOOK_WRITE_FAILURE")):
            with self.assertRaisesRegex(RuntimeError, "INJECTED_BOOK_WRITE_FAILURE"):
                self.mgr.advance({"SPY": D("49")}, at=at(1))
        self.assertEqual(self.ledger.get(KEY).status, "FILLED")
        self.assertEqual(len(self.ledger.fills(KEY)), 1)
        self.assertEqual(self.book.snapshot(), before)
        self.assertEqual(self.venue.account_snapshot().cash, D("95100"))

        self.reopen()
        self.mgr.sync(now=at(2))
        settled = self.book.snapshot()
        self.assertEqual(settled.cash, D("95100"))
        self.assertEqual(settled.reserved_cash, D(0))
        self.assertEqual(settled.positions, (("SPY", 100, D("4900")),))
        self.assertEqual(len(self.ledger.fills(KEY)), 1)
        self.mgr.sync(now=at(3))
        self.assertEqual(self.book.snapshot(), settled)
        self.assertEqual(len(self.ledger.fills(KEY)), 1)

    def test_terminal_buy_recovery_rejects_conflicting_venue_receipt(self):
        from unittest.mock import patch

        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        before = self.book.snapshot()
        with patch.object(OfflineReservations, "record_hypothetical_fill",
                          side_effect=RuntimeError("INJECTED_BOOK_WRITE_FAILURE")):
            with self.assertRaisesRegex(RuntimeError, "INJECTED_BOOK_WRITE_FAILURE"):
                self.mgr.advance({"SPY": D("49")}, at=at(1))
        view = self.ledger.get(KEY)
        receipt, = self.venue.fills(view.broker_order_id)
        self.reopen()
        with patch.object(self.venue, "fills",
                          return_value=(replace(receipt, price=D("48.99")),)):
            with self.assertRaisesRegex(RuntimeError, "TERMINAL_BUY_FILL_DIVERGENCE"):
                self.mgr.sync(now=at(2))
        self.assertEqual(self.book.snapshot(), before)
        self.assertEqual(len(self.ledger.fills(KEY)), 1)
        self.mgr.sync(now=at(3))
        self.assertEqual(self.book.snapshot().cash, D("95100"))

    def _assert_terminal_buy_reservation_divergence(self, field, value):
        import sqlite3
        from contextlib import closing
        from unittest.mock import patch

        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        with patch.object(OfflineReservations, "record_hypothetical_fill",
                          side_effect=RuntimeError("INJECTED_BOOK_WRITE_FAILURE")):
            with self.assertRaisesRegex(RuntimeError, "INJECTED_BOOK_WRITE_FAILURE"):
                self.mgr.advance({"SPY": D("49")}, at=at(1))
        # A structurally valid but contradictory durable reservation.
        self.assertIn(field, ("symbol", "qty", "limit_price", "stop_price"))
        with closing(sqlite3.connect(Path(self.tmp.name) / "book.sqlite")) as db:
            with db:
                db.execute(f"UPDATE buy_intents SET {field}=? WHERE id=?",
                           (value, KEY))
        self.reopen()
        before = self.book.snapshot()
        self.assertFalse(before.halted)
        with self.assertRaisesRegex(ValueError, "TERMINAL_BUY_RESERVATION_DIVERGENCE"):
            self.mgr.sync(now=at(2))
        self.assertEqual(self.book.snapshot(), replace(before, halted=True))
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(len(self.ledger.fills(KEY)), 1)
        self.reopen()
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(self.book.snapshot(), replace(before, halted=True))

    def test_terminal_buy_recovery_rejects_wrong_book_symbol(self):
        self._assert_terminal_buy_reservation_divergence("symbol", "QQQ")

    def test_terminal_buy_recovery_rejects_wrong_book_quantity(self):
        self._assert_terminal_buy_reservation_divergence("qty", 101)

    def test_terminal_buy_recovery_rejects_wrong_book_limit(self):
        self._assert_terminal_buy_reservation_divergence("limit_price", "51")

    def test_terminal_buy_recovery_rejects_wrong_book_stop(self):
        self._assert_terminal_buy_reservation_divergence("stop_price", "47")

    def test_terminal_buy_veto_does_not_strand_unrelated_cancel(self):
        self._assert_terminal_buy_veto_unrelated_terminal("CANCELLED")

    def test_terminal_buy_veto_does_not_strand_unrelated_expiry(self):
        self._assert_terminal_buy_veto_unrelated_terminal("EXPIRED")

    def _assert_terminal_buy_veto_unrelated_terminal(self, terminal_status):
        """Preserve the receipt veto and quarantine while releasing a zero-fill hold."""
        other = "b" * 64
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        second = self.mgr.execute_entry(self.entry(other, symbol="QQQ"),
                                        self.account(), recon=self.recon(at(1)),
                                        now=at(1), reconciled=True)
        self.assertEqual(second.status, "ACKED")
        self.assertEqual(self.book.snapshot().reserved_cash, D("10000"))

        # The local venue accepts a BUY below its protective stop; the book
        # rejects it after the ledger has recorded the final fill.
        with self.assertRaisesRegex(ValueError, "FILL_EXCEEDS_LIMITS"):
            self.mgr.advance({"SPY": D("47")}, at=at(2))
        self.assertEqual(self.ledger.get(KEY).status, "FILLED")
        self.assertEqual(len(self.ledger.fills(KEY)), 1)
        self.assertEqual(self.book.snapshot().cash, D("100000"))
        self.assertEqual(self.book.snapshot().positions, ())

        self.assertTrue(self.book.reconciliation_guarded())
        # The unresolved receipt must remain visible as an error, but it
        # must not strand a separately acknowledged terminal order.
        with self.assertRaisesRegex(ValueError, "FILL_EXCEEDS_LIMITS"):
            if terminal_status == "CANCELLED":
                self.mgr.cancel(other, now=at(3))
            else:
                self.mgr.expire_session(at=at(3))
        self.assertEqual(self.venue.order_status(second.broker_order_id).status,
                         terminal_status)
        self.assertEqual(self.ledger.get(other).status, terminal_status)
        remaining = self.book.snapshot()
        self.assertEqual(remaining.reserved_cash, D("5000"))
        self.assertEqual({row[0] for row in remaining.pending}, {KEY})
        self.assertEqual(remaining.cash, D("100000"))
        self.assertEqual(remaining.positions, ())
        self.reopen()
        with self.assertRaisesRegex(ValueError, "FILL_EXCEEDS_LIMITS|PAPER_BOOK_HALTED"):
            self.mgr.sync(now=at(4))
        self.assertEqual(self.ledger.get(other).status, terminal_status)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(self.book.snapshot().pending, remaining.pending)
        self.assertEqual(self.book.snapshot().cash, remaining.cash)
        self.assertEqual(len(self.ledger.fills(KEY)), 1)

    def test_terminal_buy_recovery_retains_loss_halt(self):
        from unittest.mock import patch

        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        with patch.object(OfflineReservations, "record_hypothetical_fill",
                          side_effect=RuntimeError("INJECTED_BOOK_WRITE_FAILURE")):
            with self.assertRaisesRegex(RuntimeError, "INJECTED_BOOK_WRITE_FAILURE"):
                self.mgr.advance({"SPY": D("49")}, at=at(1))
        self.book.halt()
        self.reopen()
        before = self.book.snapshot()
        self.assertTrue(before.halted)
        # Deliberately retains the existing fail-closed bookkeeping veto.
        # Post-halt settlement needs its own reviewed receipt capability.
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.mgr.sync(now=at(2))
        self.assertEqual(self.book.snapshot(), before)
        self.assertEqual(len(self.ledger.fills(KEY)), 1)

    def test_restart_reopens_book_and_ledger_then_syncs(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        self.mgr.advance({"SPY": D("49.50")}, at=at(1))
        before = self.ledger.get(KEY)
        book_before = self.book.snapshot()
        self.reopen()
        self.assertEqual(self.ledger.get(KEY), before)
        self.assertEqual(self.book.snapshot(), book_before)
        self.assertEqual(self.mgr.sync(now=at(2)), ())  # fully settled

    def test_exit_end_to_end_credits_proceeds(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        exit_plan = ExitPlan("PROPOSE_PAPER_SELL", "TAKE_PROFIT", D("60"),
                             "b" * 64, "SPY")
        view = self.mgr.execute_exit(exit_plan, symbol="SPY",
                                     limit_price=D("50"),
                                     recon=self.recon(at(2)), now=at(2))
        self.assertEqual(view.status, "ACKED")
        # A second exit intent beyond unreserved shares is refused.
        over = ExitPlan("PROPOSE_PAPER_SELL", "TAKE_PROFIT", D("50"), "c" * 64, "SPY")
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_POSITION"):
            self.mgr.execute_exit(over, symbol="SPY", limit_price=D("50"),
                                  recon=self.recon(at(3)), now=at(3))
        self.mgr.advance({"SPY": D("51")}, at=at(4))
        done = self.ledger.get("b" * 64)
        self.assertEqual((done.status, done.filled), ("FILLED", 60))
        book = self.book.snapshot()
        self.assertEqual(book.cash, D("95100") + D("60") * D("51"))
        self.assertEqual(book.positions, (("SPY", 40, D("1960")),))
        venue_pos = self.venue.positions()
        self.assertEqual(venue_pos[0].qty, 40)

    def test_direct_exit_rejects_subcent_limit_before_ledger_write(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        before_book = self.book.snapshot()
        before_ledger = self.ledger.orders()
        before_venue = self.venue.positions()
        plan = ExitPlan("PROPOSE_PAPER_SELL", "PROTECTIVE_EXIT", D("30"),
                        "b" * 64, "SPY")
        with self.assertRaisesRegex(ValueError, "INEXACT_PAPER_PRICE"):
            self.mgr.execute_exit(plan, symbol="SPY", limit_price=D("48.001"),
                                  recon=self.recon(at(2)), now=at(2))
        self.assertEqual(self.book.snapshot(), before_book)
        self.assertEqual(self.ledger.orders(), before_ledger)
        self.assertEqual(self.venue.positions(), before_venue)
        self.assertEqual(self.venue.open_orders(), ())
        self.assertIsNone(self.ledger.get("b" * 64))
        accepted = self.mgr.execute_exit(plan, symbol="SPY",
                                         limit_price=D("48.00"),
                                         recon=self.recon(at(2)), now=at(2))
        self.assertEqual(accepted.status, "ACKED")

    def test_daily_loss_halt_allows_only_reducing_sell(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        self.book.halt()  # stand-in for a persisted daily-loss shutdown
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.mgr.execute_entry(self.entry("c" * 64), self.account(),
                                   recon=self.recon(at(2)), now=at(2),
                                   reconciled=True)
        plan = ExitPlan("PROPOSE_PAPER_SELL", "PROTECTIVE_EXIT", D("40"),
                        "e" * 64, "SPY")
        view = self.mgr.execute_exit(plan, symbol="SPY",
                                     limit_price=D("48"),
                                     recon=self.recon(at(2)), now=at(2))
        self.assertEqual(view.status, "ACKED")
        self.mgr.advance({"SPY": D("48")}, at=at(3))
        snapshot = self.book.snapshot()
        self.assertTrue(snapshot.halted)
        self.assertEqual(snapshot.positions, (("SPY", 60, D("2940")),))
        self.assertEqual(snapshot.cash, D("97020"))

    def test_manual_halt_file_blocks_reducing_sell(self):
        from alphora.killswitch import KillSwitch
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        switch = KillSwitch(book=self.book, adapter=self.venue,
                            halt_file=Path(self.tmp.name) / "manual-halt.json")
        switch.kill("operator emergency", now=at(2))
        mgr = PaperOrderManager(book=self.book, ledger=self.ledger,
                                adapter=self.venue, killswitch=switch)
        plan = ExitPlan("PROPOSE_PAPER_SELL", "PROTECTIVE_EXIT", D("40"),
                        "e" * 64, "SPY")
        with self.assertRaisesRegex(ValueError, "KILL_SWITCH_ENGAGED"):
            mgr.execute_exit(plan, symbol="SPY", limit_price=D("48"),
                             recon=self.recon(at(3)), now=at(3))
        self.assertIsNone(self.ledger.get("e" * 64))

    def test_exit_plan_symbol_must_match_execution_symbol(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(), reconciled=True)
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        for plan_symbol in ("QQQ", None):
            plan = ExitPlan("PROPOSE_PAPER_SELL", "TAKE_PROFIT", D("10"),
                            "b" * 64, plan_symbol)
            with self.assertRaisesRegex(ValueError, "INVALID_OR_UNACTIONABLE_PLAN"):
                self.mgr.execute_exit(plan, symbol="SPY", limit_price=D("50"),
                                      recon=self.recon(at(2)), now=at(2))
            self.assertIsNone(self.ledger.get("b" * 64))

    def test_exit_without_position_fails(self):
        plan = ExitPlan("PROPOSE_PAPER_SELL", "PROTECTIVE_EXIT", D("10"),
                        "b" * 64, "SPY")
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_POSITION"):
            self.mgr.execute_exit(plan, symbol="SPY", limit_price=D("45"),
                                  recon=self.recon(), now=at())
        self.assertIsNone(self.ledger.get("b" * 64))

    def test_cancel_order_venue_path_releases_reservation(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        view = self.mgr.cancel(KEY, now=at(1))
        self.assertEqual(view.status, "CANCELLED")
        self.assertEqual(self.venue.order_status("paper-00000001").status,
                         "CANCELLED")
        self.assertEqual(self.book.snapshot().reserved_cash, D(0))
        self.assertEqual(self.mgr.cancel(KEY, now=at(2)).status,
                         "CANCELLED")

    def test_expire_session_settles_and_releases(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        expired = self.mgr.expire_session(at=at(100))
        self.assertEqual(expired, ("paper-00000001",))
        self.assertEqual(self.ledger.get(KEY).status, "EXPIRED")
        self.assertEqual(self.book.snapshot().reserved_cash, D(0))

    def test_sync_is_idempotent_and_reports_once(self):
        self.mgr.execute_entry(self.entry(), self.account(),
                               recon=self.recon(), now=at(),
                               reconciled=True)
        self.mgr.advance({"SPY": D("49")}, at=at(1))
        self.assertEqual(self.mgr.sync(now=at(2)), ())
        self.assertEqual(self.ledger.get(KEY).status, "FILLED")
        self.assertEqual(len(self.ledger.fills(KEY)), 1)
        book = self.book.snapshot()
        self.assertEqual(book.cash, D("95100"))

    def test_no_execution_vocabulary_on_manager(self):
        for banned in ("submit_order", "place_order", "cancel_order",
                       "create_order", "close_position"):
            self.assertFalse(hasattr(self.mgr, banned), banned)


if __name__ == "__main__":
    unittest.main()
