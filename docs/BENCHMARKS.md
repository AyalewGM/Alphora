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
Result's Sharpe and drawdown still include the warm-up interval and should
not be directly compared with post-warm-up SPY Sharpe/drawdown. No statistical
significance, total-return benchmark or investment alpha is claimed.

All comparisons are deterministic offline research, not broker simulation.
No Alpaca connectivity, credentials, orders, deployment or live trading.
