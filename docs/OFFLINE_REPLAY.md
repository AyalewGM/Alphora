# Candidate point-in-time offline replay bridge

This incremental research-only bridge follows merged historical/backtest PR #2 and
data-version receipt validation. LEAN PR #3 remains an **evaluation spike**:
no verified LEAN engine backtest, licensed vendor data, or professional
execution model has been run or claimed.

## Contract

`alphora.research.replay.replay_candidate_snapshot(snapshot, strategy,
expected_sha256=...)` calls the existing receipt validator a **second time**
and compares the newly computed canonical digest both to a caller-supplied
externally pinned SHA-256 and the embedded snapshot digest. It then passes
the resulting bars to the existing deterministic next-session-open single-asset
research backtester.

`compare_candidate_snapshots(strategy_snapshot, spy_snapshot, strategy,
expected_strategy_sha256=..., expected_spy_sha256=...)` separately rechecks both
datasets, requires an ETF labeled `SPY` for the benchmark, identical `as_of`
instant and session dates, then calls the existing price-only SPY comparison.

## Optional, externally supplied session-coverage check

Both replay functions now accept `expected_session_dates=(...)`, a strictly
increasing sequence of exact `datetime.date` values supplied **separately**
from the candidate snapshot. When provided, every replayed daily bar must match
this manifest in date, length and order. Missing, duplicate, reordered and
additional dates fail closed. This addresses a specific gap: a snapshot can
contain a missing exchange session and still satisfy the previous
next-*recorded*-open timestamp rule. It does **not** establish that the manifest
itself is complete or authentic, and it does not fetch an exchange calendar.

The returned `session_manifest_matched` field is `False` when no manifest is
supplied; it becomes `True` only after exact equality is checked, **not**
vendor/calendar verification. Do not interpret an unchecked replay as complete
session coverage. The replay/comparison also records `cost_bps` and `lookback`
to make these otherwise easy-to-omit research assumptions visible. Before any
performance claims, obtain an independently sourced session manifest including
holidays, early closes and halts, plus independent vendor data receipts. These
checks do not make raw data trading-ready.

Only `RAW_UNADJUSTED` input is currently permitted; adjusted or total-return
series are rejected because the simulator does not model corporate-action
cash flows. Passing this gate is **not** a claim that a raw dataset is usable
across splits or dividends. The benchmark's fully invested SPY reference
**does not comply** with Alphora's per-instrument 10% cap and exists solely
as price-only research context. The capped reference does not match time in
market, and excess returns do not establish alpha.

The research replay fixes initial **virtual** equity at **$100,000** and
maximum instrument purchase allocation at **10%**. Other approved policy
limits (0.5% planned trade risk, 2% daily loss halt, daily rebalance) still
require separate deterministic execution/portfolio enforcement and are
**NOT enforced by this simulator**. Transaction fees are a configurable
research assumption; they do not model market impact, slippage, spreads or
guaranteed stop fills.

## Critical limitations

- All first-seen and revision timestamps are **caller-supplied claims**; a
  reproducible SHA-256 is not a signature or proof of authentic vendor arrival.
  Pin hashes from independently stored vendor receipts before use.
- No independently verified exchange calendar, early-close/halt handling,
  missing-session coverage, delisted securities, membership universe,
  corporate actions, total-return/dividend accounting or survivorship checks.
  Tests deliberately use synthetic weekday fixtures, not licensed data.
- Latest-bar availability is evaluated at a caller-supplied `as_of`, not
  a source-independent clock. The code performs **offline research only**.
- Both series must have identical dated bars; such alignment does not prove
  true point-in-time membership or economic exposure comparability.
- No network, data download, prediction, broker login, order placement,
  live connection, paper adapter or deployment is part of this feature.
  Paper connectivity is **not authorized**.

## Evidence and future gates

Run `python -m unittest discover -s tests -v`; tests exercise hash
modifications, retroactive revisions, missing-session manifests, price-basis mixing, unsafe parameters
and SPY comparison alignment with synthetic data.

Before using any real-data performance result, obtain separately verified,
licensed vendor data with arrival/revision receipts, symbol/universe point-in-time
membership, complete session and corporate-action accounting; compare
price/total-return baseline assumptions and review independent
financial correctness. Before actual paper OMS, add durable state, account
reconciliation, kill switch, 0.5% planned risk and 2% daily halt enforcement
and explicit **separate** owner authorization. Real-money trading remains
prohibited without explicit future authorization.
