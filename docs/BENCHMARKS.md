# Offline SPY benchmark references — price-only research

The compare_with_spy() helper compares a strategy with two SPY **buy-and-hold
references**, not exposure-matched counterfactuals:

1. Capped SPY buy-and-hold: the same initial cash, transaction cost and
   *maximum instrument allocation* as the strategy, **but continuously
   invested after entry**, regardless of strategy's time in market.
2. Fully invested SPY buy-and-hold: 100% simulated exposure, for research
   context **only**, not compliant with Alphora's 10% per-ticker paper policy.

All series must have identical date indexes. Non-buy_hold strategies require
a lookback+1 prior-close signal before a next-session-open fill; SPY baselines
therefore begin at the aligned first eligible trade bar. The result includes
evaluation_start, the index where the price-only benchmark window begins,
and excess_vs_capped_buy_hold instead of the misleading
comparable_excess_return label.

**This excess measures returns relative to a constant capped SPY allocation,
not skill, pure alpha or matched exposure.** A strategy that exits often has
less time in market. This comparison does not control for volatility, sector
betas, portfolio exposures or intraday differences.

Results are not dividend-adjusted total returns; share splits, corporate
actions and survivorship biases remain unaddressed. Open positions are
marked-to-market at the final close without a hypothetical liquidation fee,
while actual earlier exits include simulated transaction costs. The strategy
Result's Sharpe and drawdown still include the warm-up interval and must
not be compared directly with post-warm-up SPY Sharpe/drawdown. No statistical
significance, total-return benchmark or investment alpha is claimed.


## Matched evaluation dates for descriptive risk metrics

`BenchmarkComparison` now provides `aligned_strategy`, `aligned_capped_spy`,
and `aligned_fully_invested_spy` records with `sessions`, `total_return`,
`max_drawdown`, and `sharpe`. These recalculate all three paths on the **same
post-warm-up close-to-close daily return intervals**: the strategy equity
curve is sliced from `evaluation_start` while each SPY proxy begins at its
already aligned initial cash observation. An invariant checks equal curve
lengths and that the strategy is still flat at the evaluation boundary.

`strategy`, `constrained_spy_proxy` and `fully_invested_spy_proxy` remain
unchanged legacy `Result` objects, and their `sharpe` fields are not a
same-window comparison for non-buy-and-hold strategies. The reported
`excess_vs_capped_buy_hold` uses the aligned window returns. The Sharpe
calculation assumes a zero risk-free rate, 252 annual sessions and sample
standard deviation; a flat return series yields `None`. Matching dates does
**not** match time-in-market, sector, beta, fees after terminal liquidation,
or corporate-action-adjusted returns. Neither aligned Sharpe nor excess
constitutes statistical evidence of skill or financial performance.

Aligned ratio, drawdown and daily-return calculations now use a pinned
28-significant-digit Decimal reporting context, matching the synthetic
backtest reporting policy. The displayed excess versus the capped SPY
reference is computed in that context as well. Thus the same synthetic bars
produce identical comparison figures when called from Decimal precisions 6,
28 or 80, rather than silently rounding a small excess return to zero.
Ratios and Sharpe remain finite-precision descriptive research estimates,
not audited returns; the inputs are still price-only, without verified
vendor provenance, corporate actions or a true LEAN engine run.

If a finite Decimal aligned equity series would overflow a float daily return or variance (or yield non-finite Sharpe), aligned metrics fail closed with `NONFINITE_RESEARCH_REPORT` instead of reporting an infinite/NaN or misleading zero statistic. This bound concerns synthetic numerical representability, not real-market data validation.

All comparisons are deterministic offline research, not broker simulation.
No Alpaca connectivity, credentials, orders, deployment or live trading.
