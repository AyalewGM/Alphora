"""Price-only, same-session SPY benchmarking for offline financial research.

Capped buy-hold matches only per-instrument allocation, not time in market.
The fully invested SPY proxy is NOT compliant with the owner allocation cap.
"""
from dataclasses import dataclass
from decimal import Decimal as D
from math import sqrt
from .backtest import backtest, validate, Result


@dataclass(frozen=True)
class AlignedWindowMetrics:
    """Risk metrics calculated on the same price observation dates.

    Uses a zero risk-free rate and 252 annual sessions, like the baseline
    backtester. These are not matched-exposure risk-adjusted alpha estimates.
    """
    sessions: int
    total_return: D
    max_drawdown: D
    sharpe: float | None


def _aligned_metrics(equity_curve) -> AlignedWindowMetrics:
    if len(equity_curve) < 2 or any(
        not isinstance(value, D) or not value.is_finite() or value <= 0
        for value in equity_curve
    ):
        raise ValueError("INVALID_EVALUATION_EQUITY")
    peak = equity_curve[0]
    drawdown = D(0)
    returns = []
    for before, after in zip(equity_curve, equity_curve[1:]):
        peak = max(peak, after)
        drawdown = max(drawdown, (peak - after) / peak)
        returns.append(float(after / before - 1))
    sharpe = None
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        if variance > 0:
            sharpe = sqrt(252) * mean / sqrt(variance)
    return AlignedWindowMetrics(
        len(equity_curve), equity_curve[-1] / equity_curve[0] - 1,
        drawdown, sharpe,
    )


@dataclass(frozen=True)
class BenchmarkComparison:
    strategy: Result
    constrained_spy_proxy: Result
    fully_invested_spy_proxy: Result
    excess_vs_capped_buy_hold: D
    evaluation_start: int
    aligned_strategy: AlignedWindowMetrics
    aligned_capped_spy: AlignedWindowMetrics
    aligned_fully_invested_spy: AlignedWindowMetrics


def compare_with_spy(strategy_bars, spy_bars, strategy_name, *,
                     initial=D("100000"), cost_bps=D("5"),
                     max_allocation=D("0.10"), lookback=20):
    """Compare price-only research series on one post-warm-up window.

    Capped SPY is continuously held after eligible entry; it does not match
    strategy time-in-market. Fully invested SPY is noncompliant research only.
    """
    validate(strategy_bars)
    validate(spy_bars)
    if [b.day for b in strategy_bars] != [b.day for b in spy_bars]:
        raise ValueError("BENCHMARK_DATE_MISMATCH")
    if (type(lookback) is not int or lookback < 2
            or strategy_name not in ("trend", "momentum", "mean_reversion", "buy_hold")):
        raise ValueError("INVALID_PARAMETERS")
    start = 0 if strategy_name == "buy_hold" else lookback
    if len(strategy_bars) - start < 2:
        raise ValueError("INSUFFICIENT_EVALUATION_WINDOW")
    strategy = backtest(strategy_bars, strategy_name, initial=initial,
                        cost_bps=cost_bps, max_allocation=max_allocation,
                        lookback=lookback)
    # SPY opens on comparison index 1, matching the first strategy-eligible
    # next-open fill at original index (lookback + 1).
    comparison_bars = spy_bars[start:]
    capped = backtest(comparison_bars, "buy_hold", initial=initial,
                      cost_bps=cost_bps, max_allocation=max_allocation,
                      lookback=lookback)
    full = backtest(comparison_bars, "buy_hold", initial=initial,
                    cost_bps=cost_bps, max_allocation=D("1"),
                    lookback=lookback)
    # Legacy strategy Result contains its warm-up cash period: its Sharpe and
    # max drawdown must NOT be compared with the SPY Results on shorter dates.
    aligned_curve = strategy.equity_curve[start:]
    if (len(aligned_curve) != len(capped.equity_curve)
            or len(aligned_curve) != len(full.equity_curve)
            or aligned_curve[0] != initial):
        raise ValueError("BENCHMARK_EVALUATION_WINDOW_MISMATCH")
    aligned_strategy = _aligned_metrics(aligned_curve)
    aligned_capped = _aligned_metrics(capped.equity_curve)
    aligned_full = _aligned_metrics(full.equity_curve)
    return BenchmarkComparison(
        strategy, capped, full,
        aligned_strategy.total_return - aligned_capped.total_return,
        start, aligned_strategy, aligned_capped, aligned_full,
    )
