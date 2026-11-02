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
        self.assertEqual(r.comparable_excess_return,D(0))
        self.assertGreater(r.fully_invested_spy_proxy.total_return,
                           r.constrained_spy_proxy.total_return)
    def test_date_mismatch_rejected(self):
        other=self.bars.copy()
        other[-1]=replace(other[-1],day=other[-1].day+timedelta(days=1))
        with self.assertRaisesRegex(ValueError,"BENCHMARK_DATE_MISMATCH"):
            compare_with_spy(self.bars,other,"trend")
    def test_cost_assumption_matched(self):
        r=compare_with_spy(self.bars,self.bars,"buy_hold",cost_bps=D(25))
        self.assertEqual(r.comparable_excess_return,D(0))
    def test_invalid_allocation_fails_closed(self):
        with self.assertRaises(ValueError):
            compare_with_spy(self.bars,self.bars,"trend",max_allocation=D("1.01"))
