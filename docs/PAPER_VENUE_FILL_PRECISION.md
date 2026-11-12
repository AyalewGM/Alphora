# Offline paper venue: deterministic fill arithmetic

## Defect and scope

The in-memory **simulated-only** `PaperBrokerAdapter.advance()` inherited the
calling Python `decimal` context for slippage factors, simulated cash settlement,
cost basis and fill value. At precision 3, a 37-basis-point BUY adjustment
`1.0037` rounded to `1.00`, omitting intended slippage; cash and cost basis
could also round during settlement. This is **not** a live broker or a claim
about real execution quality.

## Focused correction

All fill/slippage calculations in `advance()` now occur in a local 50-digit
Decimal context with half-even rounding, independent of caller precision.
The existing deliberate cent-price quantization, including rounding
caller-supplied subcent market observations, is preserved. The simulated fill
model still uses a single caller-asserted executable mark with no spread,
market impact, real venue data or partial-fill modelling.

The synthetic regression checks a 33-share BUY at a $49.70 mark with 37bps
slippage and $1 fee: its simulated fill price is $49.88 and paper cash becomes
$98,352.96. A subsequent 11-share SELL at a $49.80 mark fills at $49.62 and
cash becomes $98,897.78. Both must match at caller precision 3/6/28/80.

## Explicit limitations

This does **not** prove transactional atomicity of multi-order fill batches,
extreme Decimal exponent handling, same-session crash recovery, durable
multi-session open-equity rollover, order reconciliation, realistic liquidity,
or independent financial/risk QA. `advance()` still updates mark metadata
before fill processing and may require more comprehensive preflight/rollback
design for general failure atomicity. Do not describe this patch as a complete
OMS or 2% daily-loss-shutdown solution.

This is stacked on draft #47 → #46, not on the separate #43 → #44 → #45
runner changes. Release remains blocked by #42 and #20; no brokerage
connectivity, credentials, deployment, trading, live orders or automatic merges
are permitted. Preserve the approved $100,000 simulated equity, 0.5% planned
trade risk, 10% maximum allocation, 2% daily-loss stop and daily rebalance.
