"""Deterministic offline paper-venue behaviour and accounting tests."""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext

from alphora.broker.base import BrokerPosition, DispatchRequest
from alphora.broker.paper import PaperBrokerAdapter

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


def buy(symbol="SPY", qty=100, limit="50", **kw):
    return DispatchRequest(symbol, "BUY", qty, D(limit), **kw)


def sell(symbol="SPY", qty=100, limit="50", **kw):
    return DispatchRequest(symbol, "SELL", qty, D(limit), **kw)


class PaperAdapterTests(unittest.TestCase):
    def setUp(self):
        self.venue = PaperBrokerAdapter()

    def dispatch_buy(self, client="c1", **kw):
        return self.venue.dispatch(buy(**kw), client_order_id=client)

    def test_defaults_and_no_execution_vocabulary(self):
        snap = self.venue.account_snapshot()
        self.assertEqual((snap.mode, snap.equity, snap.cash),
                         ("paper", D("100000"), D("100000")))
        self.assertEqual((snap.reserved_cash, snap.available_cash), (D(0), D("100000")))
        self.assertEqual(snap.day_pnl, D(0))
        self.assertEqual(self.venue.positions(), ())
        self.assertEqual(self.venue.open_orders(), ())
        self.assertIsNone(self.venue.order_status("paper-00000001"))
        for banned in ("submit_order", "place_order", "cancel_order",
                       "create_order", "close_position"):
            self.assertFalse(hasattr(self.venue, banned), banned)

    def test_config_validation_fails_closed(self):
        for kw in ({"starting_cash": D("0")}, {"starting_cash": D("nan")},
                   {"starting_cash": 100000.0},
                   {"slippage_bps": D("-1")}, {"slippage_bps": D("10000")},
                   {"fee_per_order": D("-0.01")}):
            with self.subTest(kw=kw):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_VENUE_CONFIG"):
                    PaperBrokerAdapter(**kw)

    def test_dispatch_validation_fails_closed(self):
        invalid = (
            ("not-a-request", "c1"),
            (buy(symbol="spy;drop"), "c1"),
            (DispatchRequest("SPY", "HOLD", 1, D("50")), "c1"),
            (buy(qty=0), "c1"),
            (buy(qty=1.5), "c1"),
            (buy(limit="0"), "c1"),
            (buy(limit="nan"), "c1"),
            (DispatchRequest("SPY", "BUY", 1, D("50"), "GTC"), "c1"),
            (buy(), ""),
            (buy(), "bad id!"),
            (buy(), 42),
        )
        for req, client in invalid:
            with self.subTest(req=req, client=client):
                with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH"):
                    self.venue.dispatch(req, client_order_id=client)
        self.assertEqual(self.venue.open_orders(), ())

    def test_dispatch_dedupes_client_id_and_conflicts(self):
        first = self.dispatch_buy()
        self.assertTrue(first.accepted)
        self.assertEqual(first.broker_order_id, "paper-00000001")
        again = self.dispatch_buy()
        self.assertEqual((again.accepted, again.broker_order_id),
                         (True, "paper-00000001"))
        conflict = self.dispatch_buy(qty=90)
        self.assertEqual((conflict.accepted, conflict.broker_order_id),
                         (False, None))
        self.assertEqual(conflict.reason, "CLIENT_ORDER_ID_CONFLICT")
        self.assertEqual(len(self.venue.open_orders()), 1)

    def test_buy_reserves_limit_plus_fee_and_bounds_cash(self):
        venue = PaperBrokerAdapter(fee_per_order=D("5"))
        ack = venue.dispatch(buy(qty=100, limit="50"), client_order_id="a")
        self.assertTrue(ack.accepted)
        snap = venue.account_snapshot()
        self.assertEqual(snap.reserved_cash, D("5005"))
        self.assertEqual(snap.available_cash, D("94995"))
        # 1899 * 50 + 5 = 94,955 fits in 94,995 available; +1 share (55) doesn't.
        big = venue.dispatch(buy(symbol="QQQ", qty=1899, limit="50"),
                             client_order_id="b")
        self.assertTrue(big.accepted)
        over = venue.dispatch(buy(symbol="IWM", qty=1, limit="50"),
                              client_order_id="c")
        self.assertEqual((over.accepted, over.reason), (False, "INSUFFICIENT_CASH"))

    def test_dispatch_buying_power_ignores_ambient_decimal_precision(self):
        # Precision 3 previously rounded $100,000.01 to $100,000,
        # accepting an order exceeding the $100k cash-only account.
        venue = PaperBrokerAdapter()
        with localcontext() as ctx:
            ctx.prec = 3
            rejected = venue.dispatch(buy(qty=1, limit="100000.01"),
                                      client_order_id="over")
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "INSUFFICIENT_CASH")
        self.assertEqual(venue.open_orders(), ())
        snap = venue.account_snapshot()
        self.assertEqual((snap.cash, snap.reserved_cash, snap.available_cash),
                         (D("100000"), D(0), D("100000")))
        self.assertTrue(venue.dispatch(buy(qty=1, limit="50"),
                                       client_order_id="valid").accepted)

    def test_dispatch_and_cancel_preserve_exact_cent_reservations(self):
        # At low ambient precision two $49,999.99 holds must remain
        # $99,999.98, then unwind without a $0.01 cash accounting drift.
        venue = PaperBrokerAdapter()
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision):
                venue = PaperBrokerAdapter()
                with localcontext() as ctx:
                    ctx.prec = precision
                    for key in ("a", "b"):
                        ack = venue.dispatch(buy(qty=1, limit="49999.99"),
                                             client_order_id=key)
                        self.assertTrue(ack.accepted)
                    self.assertEqual(venue.account_snapshot().reserved_cash,
                                     D("99999.98"))
                    self.assertEqual(venue.account_snapshot().available_cash,
                                     D("0.02"))
                    venue.cancel("paper-00000001")
                    self.assertEqual(venue.account_snapshot().reserved_cash,
                                     D("49999.99"))
                    venue.cancel("paper-00000002")
                    self.assertEqual(venue.account_snapshot().reserved_cash, D(0))
                    self.assertEqual(venue.account_snapshot().cash, D("100000"))

    def test_sell_requires_unreserved_shares(self):
        venue = PaperBrokerAdapter()
        venue.dispatch(buy(qty=50, limit="10"), client_order_id="b")
        venue.advance({"SPY": D("10")}, at=at())
        short = venue.dispatch(sell(qty=51), client_order_id="s1")
        self.assertEqual((short.accepted, short.reason),
                         (False, "INSUFFICIENT_SHARES"))
        ok = venue.dispatch(sell(qty=40), client_order_id="s2")
        self.assertTrue(ok.accepted)
        again = venue.dispatch(sell(qty=11), client_order_id="s3")
        self.assertEqual((again.accepted, again.reason),
                         (False, "INSUFFICIENT_SHARES"))

    def test_buy_fills_at_mark_improvement_not_limit(self):
        self.dispatch_buy(qty=100, limit="50")
        events = self.venue.advance({"SPY": D("49.25")}, at=at(1))
        self.assertEqual(len(events), 1)
        fill = events[0]
        self.assertEqual((fill.qty, fill.price, fill.symbol, fill.side),
                         (100, D("49.25"), "SPY", "BUY"))
        self.assertEqual(fill.broker_order_id, "paper-00000001")
        self.assertEqual(fill.occurred_at, at(1))
        snap = self.venue.account_snapshot()
        self.assertEqual(snap.cash, D("95075"))       # 100 * 49.25
        self.assertEqual(snap.reserved_cash, D(0))    # reservation released
        self.assertEqual(self.venue.positions(),
                         (BrokerPosition("SPY", 100, D("49.25")),))
        view = self.venue.order_status("paper-00000001")
        self.assertEqual((view.status, view.filled, view.avg_fill_price),
                         ("FILLED", 100, D("49.25")))

    def test_no_fill_when_mark_stays_above_limit(self):
        self.dispatch_buy(limit="50")
        self.assertEqual(self.venue.advance({"SPY": D("50.01")}, at=at()), ())
        view = self.venue.order_status("paper-00000001")
        self.assertEqual(view.status, "ACCEPTED")
        # A later mark at the limit still fills (limit boundary inclusive).
        events = self.venue.advance({"SPY": D("50")}, at=at(1))
        self.assertEqual(len(events), 1)

    def test_slippage_shifts_fill_price_and_can_block_boundary(self):
        venue = PaperBrokerAdapter(slippage_bps=D("10"))  # 10 bps = 0.1%
        venue.dispatch(buy(limit="50"), client_order_id="b")
        # mark 50 * 1.001 = 50.05 > 50 -> no fill at the exact limit boundary.
        self.assertEqual(venue.advance({"SPY": D("50")}, at=at()), ())
        events = venue.advance({"SPY": D("49.95")}, at=at(1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].price, D("50.00"))  # half-even cents

    def test_fee_debited_on_fill_and_reserved_at_dispatch(self):
        venue = PaperBrokerAdapter(fee_per_order=D("7.50"))
        venue.dispatch(buy(qty=10, limit="50"), client_order_id="b")
        events = venue.advance({"SPY": D("49")}, at=at())
        self.assertEqual(events[0].fee, D("7.50"))
        self.assertEqual(venue.account_snapshot().cash, D("99502.50"))
        self.assertEqual(venue.positions(),
                         (BrokerPosition("SPY", 10, D("49.75")),))

    def test_sell_fill_credits_proceeds_and_reduces_basis(self):
        venue = PaperBrokerAdapter()
        venue.dispatch(buy(qty=100, limit="10"), client_order_id="b")
        venue.advance({"SPY": D("10")}, at=at())
        venue.dispatch(sell(qty=40, limit="12"), client_order_id="s")
        events = venue.advance({"SPY": D("12.50")}, at=at(1))
        self.assertEqual(events[0].price, D("12.50"))
        snap = venue.account_snapshot()
        self.assertEqual(snap.cash, D("99500"))  # 99000 + 40*12.50
        self.assertEqual(venue.positions(),
                         (BrokerPosition("SPY", 60, D("10")),))

    def test_cancel_releases_reservation_and_is_terminal_idempotent(self):
        ack = self.dispatch_buy()
        snap = self.venue.account_snapshot()
        self.assertEqual(snap.reserved_cash, D("5000"))
        done = self.venue.cancel(ack.broker_order_id)
        self.assertEqual(done.status, "CANCELLED")
        self.assertEqual(self.venue.account_snapshot().reserved_cash, D(0))
        again = self.venue.cancel(ack.broker_order_id)
        self.assertEqual((again.status, again.reason),
                         ("CANCELLED", "ALREADY_TERMINAL"))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_BROKER_ORDER"):
            self.venue.cancel("paper-99999999")
        with self.assertRaisesRegex(ValueError, "INVALID_BROKER_ORDER_ID"):
            self.venue.cancel("bad id!")

    def test_cancelled_order_never_fills_on_later_marks(self):
        ack = self.dispatch_buy()
        self.venue.cancel(ack.broker_order_id)
        self.assertEqual(self.venue.advance({"SPY": D("1")}, at=at()), ())

    def test_expire_session_releases_and_reports(self):
        self.dispatch_buy("a")
        second = self.venue.dispatch(buy(symbol="QQQ", qty=10, limit="100"),
                                     client_order_id="b")
        self.venue.advance({"QQQ": D("99")}, at=at())
        self.assertEqual(second.accepted, True)
        expired = self.venue.expire_session(at=at(10))
        self.assertEqual(expired, ("paper-00000001",))
        self.assertEqual(self.venue.order_status("paper-00000001").status,
                         "EXPIRED")
        self.assertEqual(self.venue.order_status("paper-00000002").status,
                         "FILLED")
        self.assertEqual(self.venue.account_snapshot().reserved_cash, D(0))
        self.assertEqual(self.venue.expire_session(at=at(11)), ())

    def test_advance_validation_and_unmarked_symbols(self):
        self.dispatch_buy("a")
        with self.assertRaisesRegex(ValueError, "INVALID_TIMESTAMP"):
            self.venue.advance({"SPY": D("49")}, at=datetime(2026, 11, 2))
        for bad in ({"spy": D("1")}, {"SPY": D("0")}, {"SPY": D("nan")},
                    {"SPY": 49.0}, "SPY", [("SPY", D("49"))]):
            with self.subTest(marks=bad):
                with self.assertRaises(ValueError):
                    self.venue.advance(bad, at=at())
        # Unmarked symbols do not fill; equity still marks other positions.
        self.assertEqual(self.venue.advance({"QQQ": D("100")}, at=at()), ())
        self.assertEqual(self.venue.order_status("paper-00000001").status,
                         "ACCEPTED")

    def test_equity_marks_positions_and_day_pnl(self):
        self.dispatch_buy(qty=100, limit="50")
        self.venue.advance({"SPY": D("50")}, at=at())
        snap = self.venue.account_snapshot()
        self.assertEqual(snap.equity, D("100000"))
        self.venue.advance({"SPY": D("45")}, at=at(1))  # mark drops; filled, no new event
        snap = self.venue.account_snapshot()
        self.assertEqual(snap.equity, D("99500"))
        self.assertEqual(snap.day_pnl, D("-500"))

    def test_account_snapshot_uses_isolated_decimal_precision(self):
        # A 200-share position at $40.01, funded from $100k with a $50
        # initial fill, is worth exactly $98,002. At caller precision 3,
        # unguarded Decimal arithmetic rounds this to $98,000, a false
        # hit of the 2% day-loss threshold.
        venue = PaperBrokerAdapter()
        venue.dispatch(buy(qty=200, limit="50"), client_order_id="b")
        venue.advance({"SPY": D("50")}, at=at())
        venue.advance({"SPY": D("40.01")}, at=at(1))
        baseline = venue.account_snapshot()
        self.assertEqual((baseline.cash, baseline.equity, baseline.day_pnl),
                         (D("90000"), D("98002"), D("-1998")))
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as ctx:
                ctx.prec = precision
                snap = venue.account_snapshot()
                self.assertEqual(snap.equity, D("98002"))
                self.assertEqual(snap.day_pnl, D("-1998"))
                self.assertEqual(snap.available_cash, D("90000"))

    def test_missing_fresh_position_mark_never_uses_cost_or_old_price(self):
        self.dispatch_buy(qty=1000, limit="50")
        self.venue.advance({"SPY": D("50")}, at=at(1))
        self.assertEqual(self.venue.account_snapshot().equity, D("100000"))
        self.venue.advance({"QQQ": D("20")}, at=at(2))
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_OR_STALE_MARK"):
            self.venue.account_snapshot()
        self.venue.advance({"SPY": D("47")}, at=at(3))
        self.assertEqual(self.venue.account_snapshot().equity, D("97000"))

    def test_fresh_mark_query_rejects_missing_future_or_stale_marks(self):
        venue = PaperBrokerAdapter()
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_OR_STALE_MARK"):
            venue.require_fresh_mark("SPY", at=at())
        venue.advance({"SPY": D("50")}, at=at(1))
        self.assertEqual(venue.require_fresh_mark("SPY", at=at(2)), D("50"))
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_OR_STALE_MARK"):
            venue.require_fresh_mark("SPY", at=at(7))
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_OR_STALE_MARK"):
            venue.require_fresh_mark("SPY", at=at())
        venue.advance({"QQQ": D("30")}, at=at(3))
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_OR_STALE_MARK"):
            venue.require_fresh_mark("SPY", at=at(3))

    def test_subcent_market_marks_cannot_create_fractional_cent_equity(self):
        venue = PaperBrokerAdapter(slippage_bps=D("23"))
        venue.dispatch(buy(qty=10, limit="50"), client_order_id="b")
        fills = venue.advance({"SPY": D("49.884961")}, at=at())
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, D("49.99"))
        account = venue.account_snapshot()
        self.assertEqual(account.cash, account.cash.quantize(D("0.01")))
        self.assertEqual(account.equity, account.equity.quantize(D("0.01")))
        self.assertEqual(venue.positions()[0].avg_price,
                         venue.positions()[0].avg_price.quantize(D("0.01")))

    def test_subcent_fees_and_order_limits_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "INVALID_PAPER_VENUE_CONFIG"):
            PaperBrokerAdapter(fee_per_order=D("0.001"))
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH"):
            self.venue.dispatch(buy(limit="49.999"), client_order_id="bad")

    def test_fill_slippage_and_cash_ignore_ambient_decimal_precision(self):
        # Precision 3 previously erased 37bps of simulated slippage and
        # rounded filled cash / basis values at the paper settlement boundary.
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision):
                venue = PaperBrokerAdapter(slippage_bps=D("37"),
                                           fee_per_order=D("1"))
                self.assertTrue(venue.dispatch(buy(qty=33, limit="50"),
                                               client_order_id="b").accepted)
                with localcontext() as ctx:
                    ctx.prec = precision
                    fills = venue.advance({"SPY": D("49.70")}, at=at())
                self.assertEqual(len(fills), 1)
                self.assertEqual(fills[0].price, D("49.88"))
                self.assertEqual(venue.account_snapshot().cash, D("98352.96"))
                self.assertTrue(venue.dispatch(sell(qty=11, limit="49"),
                                               client_order_id="s").accepted)
                with localcontext() as ctx:
                    ctx.prec = precision
                    sale = venue.advance({"SPY": D("49.80")}, at=at(1))
                self.assertEqual(len(sale), 1)
                self.assertEqual(sale[0].price, D("49.62"))
                self.assertEqual(venue.account_snapshot().cash, D("98897.78"))
                self.assertEqual(venue.positions()[0].qty, 22)

    def test_slippage_cash_and_average_basis_are_cent_quantized(self):
        venue = PaperBrokerAdapter(slippage_bps=D("37"))
        venue.dispatch(buy(qty=33, limit="50"), client_order_id="b")
        fills = venue.advance({"SPY": D("49.70")}, at=at())
        self.assertEqual(len(fills), 1)
        price = fills[0].price
        self.assertEqual(price, price.quantize(D("0.01")))
        self.assertEqual(venue.account_snapshot().cash,
                         D("100000") - price * 33)
        assert venue.account_snapshot().cash == venue.account_snapshot().cash.quantize(D("0.01"))
        venue.dispatch(sell(qty=11, limit="49"), client_order_id="s")
        sale = venue.advance({"SPY": D("49.80")}, at=at(1))
        self.assertEqual(len(sale), 1)
        self.assertEqual(sale[0].price, sale[0].price.quantize(D("0.01")))
        self.assertEqual(venue.positions()[0].avg_price,
                         venue.positions()[0].avg_price.quantize(D("0.01")))

    def test_dispatch_order_processing_is_deterministic(self):
        first = self.venue.dispatch(buy(symbol="AAA", qty=1, limit="5"),
                                    client_order_id="a")
        second = self.venue.dispatch(buy(symbol="SPY", qty=1, limit="5"),
                                     client_order_id="b")
        third = self.venue.dispatch(buy(symbol="ZZZ", qty=1, limit="5"),
                                    client_order_id="c")
        self.assertEqual([a.broker_order_id for a in (first, second, third)],
                         ["paper-00000001", "paper-00000002", "paper-00000003"])
        # Marks arrive for ZZZ and AAA only; dispatch order decides event order.
        events = self.venue.advance({"ZZZ": D("5"), "AAA": D("5")}, at=at())
        self.assertEqual([e.broker_order_id for e in events],
                         ["paper-00000001", "paper-00000003"])

    def test_sell_slippage_moves_price_down(self):
        venue = PaperBrokerAdapter(slippage_bps=D("100"))  # 1%
        venue.dispatch(buy(qty=10, limit="10"), client_order_id="b")
        venue.advance({"SPY": D("9.90")}, at=at())  # 9.90*1.01 = 9.999 <= 10
        venue.dispatch(sell(qty=10, limit="10"), client_order_id="s")
        # mark 10 * 0.99 = 9.90 < 10 limit -> no fill; at 10.11 it fills.
        self.assertEqual(venue.advance({"SPY": D("10")}, at=at(1)), ())
        events = venue.advance({"SPY": D("10.11")}, at=at(2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].price, D("10.01"))  # rounded cent


if __name__ == "__main__":
    unittest.main()
