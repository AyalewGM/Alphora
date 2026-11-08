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
credentials to this repository, including while it is private. Capture the official engine/CLI and
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

## Cross-leg metadata preflight

After obtaining one independently recorded receipt for each of the three legs,
`verify_suite((strategy, full_spy, capped_spy))` rejects missing or duplicated
legs and differing **declared** start/end sessions, calendar digests, LEAN CLI
versions or engine image digests. It returns a per-leg metadata fingerprint,
not an acceptance verdict. Algorithm, dataset, settings, and corporate-action
hashes may differ for legitimate distinct strategies and benchmark exposure
conventions; do not treat this preflight as proof of data equivalence.

An independent reviewer **must still inspect actual dated output rows**, SPY
price/total-return normalization, data coverage, fees, cash, dividends and
corporate actions for all three legs. Identical calendar hashes or claimed
session dates do not prove the output bars align or that LEAN executed.

The two SPY exposure legs require distinct benchmark receipts and financial
comparisons. This validator does not reconcile fees, slippage, split/dividend
accounting, Sharpe, out-of-sample windows, session alignment, strategy
profitability, or deviations between LEAN and the reference simulator.

## Two complete three-leg attempts

`verify_repeated_suite(first_attempt, second_attempt)` combines the existing
suite and per-leg repeat checks. Each argument must be a tuple containing one
receipt for each of the three benchmark modes. It rejects missing/duplicated
legs, differing declared engine/calendar/evaluation windows within an attempt,
and changed input or output hashes between attempts for **any** leg. It returns
three stable, mode-keyed receipt fingerprints only when all metadata agree.

**This is not six-run proof:** copies of the same fabricated receipts pass. An
independent human must witness two actual offline LEAN executions per leg,
verify command logs/exit status, bind hashes to real off-repository artifacts,
and reconcile dated financial output before declaring reproducibility.

## Test / merge boundary

`python -m unittest discover -s tests -v` should pass without network or
external dependencies. Independent human financial-data/security and QA
signoff, exact-head CI and authentic vendor evidence are still required. No
merge, deployment, paper broker connectivity, or real-money trading is
authorized by this draft feature.
