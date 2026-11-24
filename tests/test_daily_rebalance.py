import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D, localcontext

from alphora.research.backtest import Bar
from alphora.research.daily_rebalance import daily_open_reference


def bars(*prices):
    result = []
    for i, (opening, closing) in enumerate(prices):
        o, c = D(opening), D(closing)
        result.append(Bar(date(2025, 1, 2) + timedelta(days=i),
                          o, max(o, c), min(o, c), c, 1000))
    return result


class DailyReferenceTests(unittest.TestCase):
    def test_upward_gap_sell_and_close_drift(self):
        rows = daily_open_reference(
            bars((100, 100), (100, 100), (200, 300), (300, 300)),
            cost_bps=D(0)).observations
        self.assertEqual([x.shares for x in rows], [0, 100, 55, 38])
        self.assertEqual([x.trade_shares for x in rows], [0, 100, -45, -17])
        self.assertEqual(rows[2].cash, D(99000))
        self.assertEqual(rows[2].post_open_equity, D(110000))
        self.assertEqual(rows[2].open_exposure, D("0.10"))
        self.assertTrue(rows[2].pre_open_above_target)
        self.assertTrue(rows[2].close_above_target)
        self.assertEqual(rows[2].close_equity, D(115500))
        self.assertFalse(rows[3].close_above_target)

    def test_fees_cash_and_post_fee_weight(self):
        r = daily_open_reference(
            bars((100, 100), (100, 100), (200, 200)), cost_bps=D(100))
        first, second = r.observations[1:]
        self.assertEqual((first.shares, first.cash, first.fee),
                         (99, D(90001), D(99)))
        self.assertEqual((second.shares, second.cash, second.fee),
                         (54, D(98911), D(90)))
        self.assertEqual(second.close_equity, D(109711))
        self.assertEqual(r.total_fees, D(189))
        self.assertEqual(r.total_traded_notional, D(18900))
        self.assertTrue(all(x.open_exposure <= D(".1") for x in r.observations))

    def test_downward_gap_adds_shares_and_no_future_close_dependency(self):
        source = bars((100, 100), (100, 100), (50, 50), (50, 50))
        r = daily_open_reference(source, cost_bps=D(0))
        self.assertEqual(r.observations[2].shares, 190)
        self.assertEqual(r.observations[2].trade_shares, 90)
        self.assertEqual(r.observations[3].trade_shares, 0)
        changed = source.copy()
        changed[2] = replace(changed[2], close=D(200), high=D(200))
        other = daily_open_reference(changed, cost_bps=D(0))
        self.assertEqual(r.observations[2].trade_shares, other.observations[2].trade_shares)
        self.assertEqual(r.observations[2].cash, other.observations[2].cash)

    def test_context_invariance_and_input_rejections(self):
        source = bars((100, 100), (99, 105), (201, 199))
        values = []
        for precision in (6, 28, 80):
            with localcontext() as ctx:
                ctx.prec = precision
                values.append(daily_open_reference(source))
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[1], values[2])
        with self.assertRaisesRegex(ValueError, "INVALID_REBALANCE_PARAMETERS"):
            daily_open_reference(source, target=D(".1001"))
        with self.assertRaisesRegex(ValueError, "ZERO_VOLUME"):
            daily_open_reference([source[0], replace(source[1], volume=0)])
        with self.assertRaisesRegex(ValueError, "INVALID_BARS"):
            daily_open_reference([source[1], source[0]])

    def test_no_fractional_or_unaffordable_shares(self):
        r = daily_open_reference(bars((100, 100), (20000, 20000)), cost_bps=D(0))
        self.assertEqual(r.observations[-1].shares, 0)
        self.assertEqual(r.observations[-1].cash, D(100000))
        self.assertEqual(r.metrics.total_return, D(0))
        self.assertEqual(r.total_fees, D(0))
