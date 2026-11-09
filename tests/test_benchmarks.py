import unittest
from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal as D
from alphora.research.backtest import Bar
from alphora.research.benchmarks import compare_with_spy

class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.bars=[Bar(date(2025,1,1)+timedelta(days=i),D(100+i),
                       D(101+i),D(99+i),D(100+i),1000) for i in range(30)]
    def test_exposure_comparison(self):
        r=compare_with_spy(self.bars,self.bars,"buy_hold",cost_bps=D(0))
        self.assertEqual(r.excess_vs_capped_buy_hold,D(0))
        self.assertGreater(r.fully_invested_spy_proxy.total_return,
                           r.constrained_spy_proxy.total_return)
    def test_date_mismatch_rejected(self):
        other=self.bars.copy()
        other[-1]=replace(other[-1],day=other[-1].day+timedelta(days=1))
        with self.assertRaisesRegex(ValueError,"BENCHMARK_DATE_MISMATCH"):
            compare_with_spy(self.bars,other,"trend")
    def test_cost_assumption_matched(self):
        r=compare_with_spy(self.bars,self.bars,"buy_hold",cost_bps=D(25))
        self.assertEqual(r.excess_vs_capped_buy_hold,D(0))
    def test_invalid_allocation_fails_closed(self):
        with self.assertRaises(ValueError):
            compare_with_spy(self.bars,self.bars,"trend",max_allocation=D("1.01"))


    def test_trend_warmup_does_not_create_false_excess(self):
        bars=[Bar(date(2025,1,1)+timedelta(days=i),D(100+i),
                  D(101+i),D(99+i),D(100+i),1000) for i in range(60)]
        r=compare_with_spy(bars,bars,'trend',cost_bps=D(0))
        self.assertEqual(r.evaluation_start,20)
        self.assertEqual(r.excess_vs_capped_buy_hold,D(0))

    def test_different_spy_series_yield_different_excess(self):
        strategy=self.bars
        spy=[replace(b,open=b.open+D(i),high=b.high+D(i),
                     low=b.low+D(i),close=b.close+D(i)) for i,b in enumerate(self.bars)]
        r=compare_with_spy(strategy,spy,'buy_hold',cost_bps=D(0))
        self.assertNotEqual(r.excess_vs_capped_buy_hold,D(0))
        self.assertEqual(r.evaluation_start,0)

    def test_too_short_after_warmup_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_EVALUATION_WINDOW'):
            compare_with_spy(self.bars[:21],self.bars[:21],'trend')



    def test_aligned_risk_metrics_use_identical_post_warmup_sessions(self):
        from math import sqrt
        bars = [Bar(date(2025, 1, 1) + timedelta(days=i),
                    D(100 + i), D(101 + i), D(99 + i), D(100 + i), 1000)
                for i in range(60)]
        r = compare_with_spy(bars, bars, "trend", cost_bps=D(0))
        self.assertEqual(r.evaluation_start, 20)
        self.assertEqual(r.aligned_strategy.sessions, 40)
        self.assertEqual(r.aligned_capped_spy.sessions, 40)
        self.assertEqual(r.aligned_fully_invested_spy.sessions, 40)
        window = r.strategy.equity_curve[20:]
        self.assertEqual(r.aligned_strategy.total_return, window[-1] / window[0] - 1)
        self.assertEqual(r.aligned_strategy.total_return,
                         r.aligned_capped_spy.total_return)
        self.assertEqual(r.excess_vs_capped_buy_hold, D(0))
        daily = [float(b / a - 1) for a, b in zip(window, window[1:])]
        avg = sum(daily) / len(daily)
        variance = sum((v - avg) ** 2 for v in daily) / (len(daily) - 1)
        self.assertAlmostEqual(r.aligned_strategy.sharpe,
                               sqrt(252) * avg / sqrt(variance))
        # Legacy strategy Result includes 20 warm-up cash rows and must not
        # be presented as comparable Sharpe against a shorter SPY baseline.
        self.assertNotAlmostEqual(r.strategy.sharpe, r.aligned_strategy.sharpe)

    def test_buy_hold_aligned_metrics_equal_full_window_metrics(self):
        r = compare_with_spy(self.bars, self.bars, "buy_hold",
                             cost_bps=D("5"))
        self.assertEqual(r.evaluation_start, 0)
        self.assertEqual(r.aligned_strategy.sessions, len(self.bars))
        self.assertEqual(r.aligned_strategy.total_return,
                         r.strategy.total_return)
        self.assertEqual(r.aligned_strategy.max_drawdown,
                         r.strategy.max_drawdown)
        self.assertEqual(r.aligned_strategy.sharpe, r.strategy.sharpe)

    def test_flat_strategy_sharpe_is_none_not_fabricated(self):
        falling = [Bar(date(2025, 1, 1) + timedelta(days=i),
                       D(200 - i), D(201 - i), D(199 - i),
                       D(200 - i), 1000) for i in range(60)]
        r = compare_with_spy(falling, falling, "trend",
                             cost_bps=D("5"))
        self.assertIsNone(r.aligned_strategy.sharpe)
        self.assertEqual(r.aligned_strategy.total_return, D(0))
        self.assertEqual(r.aligned_strategy.max_drawdown, D(0))
        self.assertEqual(r.aligned_strategy.sessions, 40)
