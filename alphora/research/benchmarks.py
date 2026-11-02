"""Price-only benchmark comparisons on aligned offline evaluation windows.

The capped SPY buy-and-hold reference matches allocation limits, NOT the
strategy's time-in-market. It must never be represented as matched exposure.
"""
from dataclasses import dataclass
from decimal import Decimal as D
from .backtest import backtest, validate, Result

@dataclass(frozen=True)
class BenchmarkComparison:
    strategy: Result
    constrained_spy_proxy: Result
    fully_invested_spy_proxy: Result
    excess_vs_capped_buy_hold: D
    evaluation_start: int

def compare_with_spy(strategy_bars, spy_bars, strategy_name, *,
                     initial=D("100000"), cost_bps=D("5"),
                     max_allocation=D("0.10"), lookback=20):
    """Compare total returns after strategy eligibility, not strategy alpha.

    Both SPY baselines enter on the first strategy-eligible next-open bar.
    Capped buy-and-hold is invested continuously once eligible and is NOT
    time-in-market/exposure matched to a strategy with intermittent holdings.
    """
    validate(strategy_bars)
    validate(spy_bars)
    if [b.day for b in strategy_bars] != [b.day for b in spy_bars]:
        raise ValueError("BENCHMARK_DATE_MISMATCH")
    if (type(lookback) is not int or lookback < 2
            or strategy_name not in ("trend","momentum","mean_reversion","buy_hold")):
        raise ValueError("INVALID_PARAMETERS")
    start=0 if strategy_name == "buy_hold" else lookback
    if len(strategy_bars) - start < 2:
        raise ValueError("INSUFFICIENT_EVALUATION_WINDOW")
    strategy=backtest(strategy_bars,strategy_name,initial=initial,
                      cost_bps=cost_bps,max_allocation=max_allocation,lookback=lookback)
    # A SPY buy-hold signal fills on index 1, i.e. the same index on which
    # a lookback strategy first has a fully formed signal (lookback+1).
    comparison_bars=spy_bars[start:]
    capped=backtest(comparison_bars,"buy_hold",initial=initial,
                    cost_bps=cost_bps,max_allocation=max_allocation,lookback=lookback)
    full=backtest(comparison_bars,"buy_hold",initial=initial,
                  cost_bps=cost_bps,max_allocation=D("1"),lookback=lookback)
    return BenchmarkComparison(strategy,capped,full,
                               strategy.total_return-capped.total_return,start)
