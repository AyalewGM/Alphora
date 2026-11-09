"""Offline forward-paper session runner: multi-day pipeline behaviour."""
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from alphora.broker.paper import PaperBrokerAdapter
from alphora.orders import OrderLedger
from alphora.research.backtest import Bar
from alphora.reservations import OfflineReservations
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

    def test_entry_hold_protective_exit_then_loss_halt(self):
        report = self.runner.run((
            day(2, "50", {"SPY": WATCH}),
            day(3, "52"),
            day(4, "40"),
            day(5, "41", {"SPY": WATCH}),
        ))
        d1, d2, d3, d4 = report.days
        # Day 1: risk sizing -> 200 shares at 10% cap, filled at the mark.
        self.assertIn("ENTRY:SPY:ACKED", d1.actions)
        self.assertEqual(d1.positions, (("SPY", 200, D("10000")),))
        self.assertEqual(d1.cash, D("90000"))
        # Day 2: held, marked up.
        self.assertEqual(d2.equity, D("100400"))
        self.assertEqual(d2.positions[0][1], 200)
        # Day 3: bid 40 <= stop 48 -> protective exit fills at mark 40.
        self.assertIn("EXIT:SPY:ACKED", d3.actions)
        self.assertEqual(d3.positions, ())
        self.assertEqual(d3.cash, D("98000"))
        self.assertTrue(d3.halted)  # halt arms right after the exit settles
        # Day 4: the armed halt vetoes any new intent.
        self.assertIn("ENTRY_VETO:SPY:PAPER_BOOK_HALTED", d4.actions)
        self.assertTrue(d4.halted)
        self.assertTrue(report.halted)
        self.assertEqual(report.final_equity, D("98000"))
        self.assertEqual(self.venue.positions(), ())

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
        self.assertEqual(d.recon_status, "FATAL")
        self.assertTrue(any(a.startswith("ENTRY_VETO:SPY")
                            for a in d.actions), d.actions)

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
