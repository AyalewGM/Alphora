import unittest
from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal as D, localcontext
from alphora.research.backtest import Bar
from alphora.research.benchmarks import _aligned_metrics, compare_with_spy

class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.bars=[Bar(date(2025,1,1)+timedelta(days=i),D(100+i),
                       D(101+i),D(99+i),D(100+i),1000) for i in range(30)]
    def test_exposure_comparison(self):
        r=compare_with_spy(self.bars,self.bars,"buy_hold",cost_bps=D(0))
        self.assertEqual(r.excess_vs_capped_buy_hold,D(0))
        self.assertGreater(r.fully_invested_spy_proxy.total_return,
                           r.constrained_spy_proxy.total_return)
    def test_initial_allocation_buy_hold_can_drift_above_cap(self):
        # The legacy benchmark is an initial-entry budget, not an ongoing
        # marked exposure control. Synthetic prices, no fees or dividends.
        bars = [
            Bar(date(2025, 1, 2), D("100"), D("100"), D("100"), D("100"), 1000),
            Bar(date(2025, 1, 3), D("100"), D("100"), D("100"), D("100"), 1000),
            Bar(date(2025, 1, 6), D("200"), D("200"), D("200"), D("200"), 1000),
        ]
        result = compare_with_spy(bars, bars, "buy_hold", cost_bps=D(0))
        proxy = result.constrained_spy_proxy
        self.assertEqual(proxy.transactions, 1)
        self.assertEqual(proxy.equity_curve, (D("100000"), D("100000"), D("110000")))
        self.assertEqual(proxy.total_return, D("0.1"))
        # 100 shares bought at $100, now $20k; unchanged $90k cash.
        marked_value = proxy.final_equity - D("90000")
        self.assertEqual(marked_value, D("20000"))
        self.assertGreater(marked_value, D("0.10") * proxy.final_equity)

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

    def test_aligned_reporting_ignores_ambient_decimal_precision(self):
        # At 6-digit ambient precision the same small positive result
        # previously reported zero return and no Sharpe, unlike precision 28.
        curve = (D("100000"), D("100000.003"),
                 D("100000.001"), D("100000.004"))
        observations = []
        for precision in (6, 28, 80):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    result = _aligned_metrics(curve)
                self.assertEqual(result.total_return, D("0.00000004"))
                self.assertGreater(result.max_drawdown, D(0))
                self.assertIsNotNone(result.sharpe)
                observations.append(result)
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[1], observations[2])

    def test_spy_comparison_excess_is_decimal_precision_invariant(self):
        # A tiny strategy/SPY excess used to round to zero for precision-6
        # callers, despite identical exact input bars and simulated fills.
        days = (date(2025, 1, 2), date(2025, 1, 3),
                date(2025, 1, 6), date(2025, 1, 7))
        def make_bars(closes):
            return [Bar(day, D("1"), D("1.0000005"),
                        D("0.9999995"), D(close), 1000)
                    for day, close in zip(days, closes)]
        strategy = make_bars(("1", "1.0000002", "1.0000001",
                              "1.0000003"))
        spy = make_bars(("1", "1.0000001", "1.0000001",
                        "1.0000001"))
        comparisons = []
        for precision in (6, 28, 80):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    result = compare_with_spy(
                        strategy, spy, "buy_hold", cost_bps=D(0))
                self.assertEqual(result.excess_vs_capped_buy_hold,
                                 D("0.00000002"))
                self.assertEqual(result.aligned_strategy.total_return,
                                 D("0.00000003"))
                self.assertEqual(result.aligned_capped_spy.total_return,
                                 D("0.00000001"))
                comparisons.append(result)
        self.assertEqual(comparisons[0], comparisons[1])
        self.assertEqual(comparisons[1], comparisons[2])

    def test_extreme_finite_aligned_equity_rejects_float_overflow(self):
        # Decimal equity remains finite but float reporting overflows:
        # the first series produces an infinite period return, the second
        # produces finite period returns with infinite sample variance.
        for curve in (
            (D("100000"), D("1e314")),
            (D("100000"), D("1e205"), D("1e205")),
        ):
            with self.subTest(curve=curve):
                with self.assertRaisesRegex(ValueError, "NONFINITE_RESEARCH_REPORT"):
                    _aligned_metrics(curve)

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
