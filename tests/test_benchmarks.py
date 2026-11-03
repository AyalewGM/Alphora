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

