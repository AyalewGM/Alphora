# Offline LEAN evidence receipt — bounded support for issue #20

`alphora.research.lean_evidence` is a **pure metadata validator** for future
isolated, licensed, locally executed LEAN backtests. It neither runs LEAN nor
connects to a market data provider or brokerage. No API keys, URLs, orders,
trading adapters or deployment paths are introduced. Do not use these functions
to claim that a backtest occurred, that data were licensed, or that returns are
accurate. The unit tests use fabricated SHA-256 digests and dates only.

## Required independently obtained inputs

A `LeanBacktestReceipt` records the exact *backtest* mode, which evaluation
leg was used (`strategy`, `spy-fully-invested-total-return`, or
`spy-10pct-and-cash`), evaluation start/end sessions, CLI version, pinned
engine container image digest, and separately measured SHA-256 digests for:

- algorithm source, actual offline dataset and exchange calendar;
- corporate-action inputs and **external** license authorization receipt;
- fees/slippage/algorithm settings, point-in-time input manifest, and LEAN
  output artifact.

These digests are metadata asserted by the caller, not authenticated by this
module. They must be generated and verified against off-repository files by a
qualified, independent data owner; an arbitrary or duplicated 64-hex string
provides no proof. Never commit vendor data, authorization documents or
credentials to this public repository. Capture the official engine/CLI and
actual local command/exit status in a separately secured run journal; this
validator does **not** execute commands or validate those claims.

## Reproducibility check

Record two **independently executed** runs after completing the issue #20
data and corporate-action gates. `verify_repeat(first, second)` fails if
recorded input hashes, modes, versions, settings or evaluation dates differ, or
if the two recorded output artifact hashes differ. It returns a stable
canonical receipt fingerprint on success. Identical duplicated or fabricated
receipts also pass; an independent operator must inspect two actual LEAN run
logs, exit codes, source dataset hashes, portfolio accounting and numerical
output. LEAN outputs with run-specific timestamps may require independently
specified deterministic normalization **before** hashing; do not silently
accept or conceal a mismatch.

The two SPY exposure legs require distinct benchmark receipts and financial
comparisons. This validator does not reconcile fees, slippage, split/dividend
accounting, Sharpe, out-of-sample windows, session alignment, strategy
profitability, or deviations between LEAN and the reference simulator.

## Test / merge boundary

`python -m unittest discover -s tests -v` should pass without network or
external dependencies. Independent human financial-data/security and QA
signoff, exact-head CI and authentic vendor evidence are still required. No
merge, deployment, paper broker connectivity, or real-money trading is
authorized by this draft feature.
