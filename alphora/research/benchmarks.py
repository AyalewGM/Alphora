"""Explicit exposure-comparable offline benchmarks; no market connectivity.

These are *price-only* comparisons, not dividend-adjusted total return indexes.
"""
from dataclasses import dataclass
from decimal import Decimal as D
from .backtest import backtest, validate, Result

@dataclass(frozen=True)
class BenchmarkComparison:
    strategy: Result
    constrained_spy_proxy: Result
    fully_invested_spy_proxy: Result
    comparable_excess_return: D

def compare_with_spy(strategy_bars, spy_bars, strategy_name, *,
                     initial=D("100000"), cost_bps=D("5"),
                     max_allocation=D("0.10"), lookback=20):
    """Use date-aligned series and separate comparable vs fully invested baselines.

    This is intended only for a single-instrument strategy on a data period
    matching SPY; avoid interpreting results as benchmark-relative alpha.
    """
    validate(strategy_bars)
    validate(spy_bars)
    if [b.day for b in strategy_bars] != [b.day for b in spy_bars]:
        raise ValueError("BENCHMARK_DATE_MISMATCH")
    strategy=backtest(strategy_bars,strategy_name,initial=initial,
                      cost_bps=cost_bps,max_allocation=max_allocation,lookback=lookback)
    comparable=backtest(spy_bars,"buy_hold",initial=initial,
                        cost_bps=cost_bps,max_allocation=max_allocation,lookback=lookback)
    full=backtest(spy_bars,"buy_hold",initial=initial,
                  cost_bps=cost_bps,max_allocation=D("1"),lookback=lookback)
    return BenchmarkComparison(strategy,comparable,full,
                               strategy.total_return-comparable.total_return)
