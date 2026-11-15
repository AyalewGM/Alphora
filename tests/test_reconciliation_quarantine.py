"""Synthetic crash/quarantine regressions; not vendor or rollover acceptance.

The provisional one-session guard is not a substitute for durable exchange-
session opening equity, actual marks, or independent multi-session QA (#42).
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D, localcontext
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from alphora.broker.base import DispatchRequest
from alphora.broker.paper import PaperBrokerAdapter
from alphora.orders import OrderLedger
from alphora.research.backtest import Bar
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount, RiskPolicy
from alphora.runner import PaperSessionRunner, SessionDay, SessionSignal


SESSION = "2026-11-02"
WATCH = SessionSignal("WATCHLIST", "VERIFIED")


def session_day(day=2, *, enter=True):
    value = D("50")
    bar = Bar(date(2026, 11, day), value, value, value, value, 1000)
    return SessionDay(
        date(2026, 11, day).isoformat(),
        datetime(2026, 11, day, 21, tzinfo=timezone.utc),
        {"SPY": bar},
        {"SPY": WATCH} if enter else {},
    )


class DurableReconciliationQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.book_path = root / "book.sqlite"
        self.ledger_path = root / "ledger.sqlite"
        self.venue = PaperBrokerAdapter()
        self._open()

    def _open(self):
        self.book = OfflineReservations(self.book_path, session_id=SESSION)
        self.ledger = OrderLedger(self.ledger_path, session_id=SESSION)
        self.runner = PaperSessionRunner(
            book=self.book, ledger=self.ledger, adapter=self.venue,
            take_profit_pct=D("50"))

    def _restart(self, *, cold_venue=False):
        self.book.close()
        self.ledger.close()
        if cold_venue:
            self.venue = PaperBrokerAdapter()
        self._open()

    def tearDown(self):
        if self.book is not None:
            self.book.close()
        self.ledger.close()
        self.directory.cleanup()

    def _account(self):
        return PaperAccount(D("100000"), D("100000"),
                            self.book.snapshot().cash, D("0"), SESSION)

    def _proposal(self):
        return BuyProposal("SPY", D("20"), D("50"), D("48"))

    def test_safe_loss_observation_during_check_does_not_terminally_halt(self):
        token = self.book.begin_reconciliation()
        self.assertTrue(self.book.snapshot().halted)  # temporary BUY veto
        observed = self.book.observe_daily_loss(self._account(), reconciled=True)
        self.assertTrue(observed.halted)
        self.assertEqual(self.book._meta("halted"), "0")
        self.assertFalse(self.book.finish_reconciliation(token, fatal=False))
        self.assertFalse(self.book.snapshot().halted)

    def test_loss_observed_during_check_survives_clean_and_restart(self):
        self.book.reserve_buy(
            "loss-position", self._account(),
            BuyProposal("SPY", D("100"), D("100"), D("99")),
            reconciled=True,
        )
        self.book.record_hypothetical_fill(
            "loss-position", "loss-fill", 100, D("100")
        )
        self.assertEqual(self.book.snapshot().cash, D("90000"))
        token = self.book.begin_reconciliation()
        observed = self.book.observe_daily_loss(
            PaperAccount(D("97900"), D("100000"), D("90000"),
                         D("-2100"), SESSION),
            reconciled=True,
        )
        self.assertTrue(observed.halted)
        self.assertEqual(self.book._meta("halted"), "1")
        self.assertFalse(self.book.finish_reconciliation(token, fatal=False))
        self.assertTrue(self.book.snapshot().halted)
        self._restart()
        self.assertEqual(self.book._meta("halted"), "1")
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy(
                "after-loss-clean", self._account(),
                self._proposal(), reconciled=True,
            )

    def test_clean_transaction_stays_dispatchable(self):
        token = self.book.begin_reconciliation()
        self.assertEqual(len(token), 32)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("no-buy-during-check", self._account(),
                                  self._proposal(), reconciled=True)
        self.assertFalse(self.book.finish_reconciliation(token, fatal=False))
        self.assertFalse(self.book.reconciliation_guarded())
        self.assertFalse(self.book.snapshot().halted)
        approved = self.book.reserve_buy("after-clean", self._account(),
                                         self._proposal(), reconciled=True)
        self.assertEqual(len(approved.pending), 1)

    def test_interrupted_check_stays_guarded_across_restart(self):
        token = self.book.begin_reconciliation()
        self.assertIsNotNone(token)
        original = self.book.snapshot()
        self._restart()
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(self.book.snapshot(), original)
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("blocked-replay", self._account(),
                                  self._proposal(), reconciled=True)
        self.assertTrue(self.book.snapshot().halted)

    def test_fatal_guard_survives_clean_rechecks_and_allows_acked_fill_settlement(self):
        self.book.reserve_buy("accepted", self._account(),
                              self._proposal(), reconciled=True)
        token = self.book.begin_reconciliation()
        self.assertTrue(self.book.finish_reconciliation(token, fatal=True))
        self.assertTrue(self.book.snapshot().halted)
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        # The quarantine vetoes NEW BUYs, but does not prematurely write
        # the terminal book halt and thereby destroy accepted fill recovery.
        landed = self.book.record_hypothetical_fill(
            "accepted", "accepted-fill", 20, D("50"))
        self.assertEqual(landed.positions, (("SPY", 20, D("1000")),))
        self.assertEqual(landed.cash, D("99000"))
        self.assertTrue(landed.halted)
        self.book.halt()
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("no-new-entry", self._account(),
                                  self._proposal(), reconciled=True)

    def test_runner_fatal_crash_before_verdict_remains_one_way_guarded(self):
        self.venue.dispatch(
            DispatchRequest("QQQ", "BUY", 1, D("10")),
            client_order_id="z" * 64)
        # Force an interruption AFTER reconcile() observed FATAL, BEFORE
        # its verdict is committed. The preceding CHECKING marker must be
        # durable, and a restarted process cannot reinterpret it as CLEAN.
        def interrupted(*_args, **_kwargs):
            raise RuntimeError("INJECTED_POST_RECONCILIATION_CRASH")

        self.book.finish_reconciliation = interrupted
        with self.assertRaisesRegex(RuntimeError, "INJECTED_POST"):
            self.runner.run((session_day(),))
        self.assertTrue(self.book.reconciliation_guarded())
        before_orders = self.ledger.orders()
        self._restart()  # preserve rogue in-memory venue for evidence
        self.assertTrue(self.book.snapshot().halted)
        result = self.runner.run((session_day(),))
        self.assertEqual(result.days[0].recon_status, "FATAL")
        self.assertTrue(result.halted)
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(self.ledger.orders(), before_orders)
        self.assertFalse(
            any(a.startswith("ENTRY:") for a in result.days[0].actions))
        self._restart()
        self.assertTrue(self.book.snapshot().halted)

    def test_cold_venue_replay_latches_fatal_without_rewriting_ledger(self):
        first = self.runner.run((session_day(),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        old_book, old_orders = self.book.snapshot(), self.ledger.orders()
        self._restart(cold_venue=True)
        replay = self.runner.run((session_day(),))
        self.assertEqual(replay.days[0].recon_status, "FATAL")
        self.assertTrue(replay.halted)
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(
            replace(self.book.snapshot(), halted=old_book.halted), old_book)
        self.assertEqual(self.ledger.orders(), old_orders)
        self.assertFalse(
            any(a.startswith(("ENTRY:", "EXIT:"))
                for a in replay.days[0].actions))
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        with self.assertRaisesRegex(ValueError, "SESSION_ROLLOVER_UNSUPPORTED"):
            self.runner.run((session_day(3),))

    def test_clean_runner_releases_guard_after_every_check(self):
        report = self.runner.run((session_day(),))
        self.assertEqual(report.days[0].recon_status, "CLEAN")
        self.assertFalse(report.halted)
        self.assertFalse(self.book.reconciliation_guarded())
        self._restart()
        self.assertFalse(self.book.reconciliation_guarded())

    def test_missing_versioned_guard_does_not_silently_reseed_clear(self):
        self.book.close()
        with sqlite3.connect(self.book_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            db.execute("DELETE FROM reconciliation_guard")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.book_path, session_id=SESSION)
        self.book = None

    def test_concurrent_check_cannot_clear_other_check_token(self):
        first = self.book.begin_reconciliation()
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        with self.assertRaisesRegex(ValueError, "RECONCILIATION_GUARD_CONFLICT"):
            self.book.finish_reconciliation("a" * 32, fatal=False)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertFalse(self.book.finish_reconciliation(first, fatal=False))
        self.assertFalse(self.book.reconciliation_guarded())


    def _simulate_existing_v0_book(self):
        """Remove only the proposed guard schema, retaining legacy history."""
        self.book.close()
        self.ledger.close()
        with sqlite3.connect(self.book_path) as db:
            db.execute("DROP TABLE reconciliation_guard")
            db.execute("PRAGMA user_version=0")
        self._open()

    def test_pristine_new_book_starts_clear_with_versioned_guard(self):
        self.assertFalse(self.book.reconciliation_guarded())
        with sqlite3.connect(self.book_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT state,token FROM reconciliation_guard").fetchone(),
                ("CLEAR", None),
            )

    def test_v0_existing_flat_book_is_quarantined_not_reset_to_clear(self):
        # Even a flat ledger cannot prove an earlier reconciliation was CLEAN.
        prior = self.book.snapshot()
        self._simulate_existing_v0_book()
        guarded = self.book.snapshot()
        self.assertEqual(replace(guarded, halted=prior.halted), prior)
        self.assertTrue(guarded.halted)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertIsNone(self.book.begin_reconciliation())
        self.assertTrue(self.book.finish_reconciliation(None, fatal=False))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("migrated-flat-block", self._account(),
                                  self._proposal(), reconciled=True)
        self._restart()
        self.assertTrue(self.book.reconciliation_guarded())

    def test_v0_pending_intent_preserves_settlement_but_quarantines_new_buys(self):
        prior = self.book.reserve_buy("accepted", self._account(),
                                      self._proposal(), reconciled=True)
        self._simulate_existing_v0_book()
        guarded = self.book.snapshot()
        self.assertEqual(replace(guarded, halted=prior.halted), prior)
        self.assertTrue(guarded.halted)
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("new-after-upgrade", self._account(),
                                  self._proposal(), reconciled=True)
        filled = self.book.record_hypothetical_fill(
            "accepted", "accepted-fill", 20, D("50"))
        self.assertEqual(filled.positions, (("SPY", 20, D("1000")),))
        self.assertEqual(filled.cash, D("99000"))
        self.assertTrue(filled.halted)
        self._restart()
        self.assertTrue(self.book.snapshot().halted)
        self.assertEqual(self.book.snapshot().cash, D("99000"))

    def test_v0_completed_and_cancelled_history_never_bootstraps_clear(self):
        self.book.reserve_buy("accepted", self._account(),
                              self._proposal(), reconciled=True)
        self.book.record_hypothetical_fill("accepted", "fill", 10, D("50"))
        self.book.cancel_remaining("accepted")
        prior = self.book.snapshot()
        self._simulate_existing_v0_book()
        after = self.book.snapshot()
        self.assertEqual(replace(after, halted=prior.halted), prior)
        self.assertTrue(after.halted)
        self.assertEqual(after.positions, (("SPY", 10, D("500")),))
        with self.assertRaisesRegex(ValueError, "PAPER_BOOK_HALTED"):
            self.book.reserve_buy("new-after-history", self._account(),
                                  self._proposal(), reconciled=True)

    def test_v0_existing_one_way_halt_is_not_cleared_by_upgrade(self):
        self.book.halt()
        self._simulate_existing_v0_book()
        self.assertTrue(self.book.snapshot().halted)
        self.assertTrue(self.book.reconciliation_guarded())
        self._restart()
        self.assertTrue(self.book.snapshot().halted)

    def test_v0_missing_meta_is_corruption_not_new_portfolio(self):
        self.book.close()
        with sqlite3.connect(self.book_path) as db:
            db.execute("DROP TABLE reconciliation_guard")
            db.execute("PRAGMA user_version=0")
            db.execute("DELETE FROM book_meta")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.book_path, session_id=SESSION)
        # The old connection is already closed, including for tearDown.

    def test_stacked_risk_policy_preserves_main_hard_ceilings(self):
        # Branch ancestry from #58 must not reintroduce 2% trade risk or
        # 100% symbol exposure. Direct constructor checks apply at all
        # caller Decimal precisions, including just-over-the-cap proposals.
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as ctx:
                ctx.prec = precision
                self.assertEqual(RiskPolicy().fingerprint(), "0.005|0.1")
                for per_trade, instrument in (
                    ("0.0050000001", "0.10"),
                    ("0.005", "0.1000000001"),
                    ("0.01", "0.20"),
                    ("0.02", "1"),
                ):
                    with self.subTest(per_trade=per_trade, instrument=instrument):
                        with self.assertRaisesRegex(ValueError, "INVALID_RISK_POLICY"):
                            RiskPolicy(D(per_trade), D(instrument))
                self.assertEqual(
                    RiskPolicy(D("0.004"), D("0.08")).fingerprint(), "0.004|0.08")

    def test_pending_same_symbol_reservations_still_enforce_ten_percent_cap(self):
        # A separate risk-policy object cannot bypass this durable book
        # check, including not-yet-filled BUY exposure and fee reservations.
        self.book.reserve_buy("first", self._account(),
                              BuyProposal("SPY", D("100"), D("50"), D("48")),
                              reconciled=True)
        prior = self.book.snapshot()
        with self.assertRaisesRegex(ValueError, "INSTRUMENT_ALLOCATION_EXCEEDED"):
            self.book.reserve_buy("over-cap", self._account(),
                                  BuyProposal("SPY", D("101"), D("50"), D("48")),
                                  reconciled=True)
        self.assertEqual(self.book.snapshot(), prior)


    def test_rogue_venue_order_is_quarantined_before_advance_or_expire(self):
        # An unknown synthetic venue order must not fill or disappear through
        # automatic expiration before its first FATAL reconciliation.
        self.venue.dispatch(
            DispatchRequest("QQQ", "BUY", 1, D("10")),
            client_order_id="z" * 64)
        prior_venue = self.venue.open_orders()
        prior_book, prior_ledger = self.book.snapshot(), self.ledger.orders()
        with (patch.object(self.runner._mgr, "advance",
                           side_effect=AssertionError("ADVANCED_BEFORE_RECON")),
              patch.object(self.runner._mgr, "expire_session",
                           side_effect=AssertionError("EXPIRED_BEFORE_RECON"))):
            report = self.runner.run((session_day(),))
        self.assertEqual(report.days[0].recon_status, "FATAL")
        self.assertTrue(report.halted)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(self.venue.open_orders(), prior_venue)
        self.assertEqual(self.ledger.orders(), prior_ledger)
        self.assertEqual(
            replace(self.book.snapshot(), halted=prior_book.halted),
            prior_book)
        self.assertFalse(any(a.startswith(("ENTRY:", "EXIT:", "FILL:"))
                             for a in report.days[0].actions))
        self._restart()
        self.assertTrue(self.book.snapshot().halted)

    def test_cold_restart_is_quarantined_before_venue_mark_advance(self):
        # Keep accepted fills in the book, lose only simulated venue memory.
        first = self.runner.run((session_day(),))
        self.assertIn("ENTRY:SPY:ACKED", first.days[0].actions)
        recorded_book, recorded_orders = (
            self.book.snapshot(), self.ledger.orders())
        self._restart(cold_venue=True)
        with patch.object(self.runner._mgr, "advance",
                          side_effect=AssertionError("ADVANCED_BEFORE_RECON")):
            report = self.runner.run((session_day(),))
        self.assertEqual(report.days[0].recon_status, "FATAL")
        self.assertTrue(report.halted)
        self.assertTrue(self.book.reconciliation_guarded())
        self.assertEqual(
            replace(self.book.snapshot(), halted=recorded_book.halted),
            recorded_book)
        self.assertEqual(self.ledger.orders(), recorded_orders)
        self.assertEqual(self.venue.positions(), ())
        self._restart()
        self.assertTrue(self.book.snapshot().halted)

if __name__ == "__main__":
    unittest.main()
