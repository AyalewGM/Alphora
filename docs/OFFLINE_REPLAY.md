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
session coverage.

The replay and comparison results also preserve
`session_manifest_sha256` (recomputed from supplied dates) and
`session_manifest_session_count` (number of supplied sessions). Both are
`None` when no manifest was supplied. These fields make the exact calendar
manifest independently comparable across audit reports even when its original
file is no longer in memory. A computed receipt can exist **without** an
externally pinned receipt: check `session_manifest_digest_matched` separately,
and do not treat either field as proof of vendor/exchange authenticity.


### Optional independently pinned calendar-manifest digest

Both replay functions also accept `expected_session_manifest_sha256=...`. The
caller must keep this lowercase SHA-256 **separately** from the bar snapshot
and the session-date manifest. To produce it deterministically, use
`session_manifest_sha256(expected_session_dates)` before independently
retaining the receipt. Canonical bytes are the ASCII prefix
`alphora-session-manifest-v1` followed by a newline and strictly ordered
`YYYY-MM-DD` dates joined by newlines, with no trailing newline.

When supplied, a missing manifest, malformed hash or changed manifest hash
fails closed. `session_manifest_digest_matched` reports whether this **hash
equality** was checked; it remains `False` for an unpinned manifest, even
when `session_manifest_matched` is `True`. Pinned means only exact receipt
reproducibility. Hashing a manifest derived from the same bars and pinning the
hash alongside them is not independent evidence, and SHA-256 cannot establish
exchange calendars, early closes, holidays, halts, vendor trust, completeness
or license rights. The caller remains responsible for independent provenance. The replay/comparison also records `cost_bps` and `lookback`
to make these otherwise easy-to-omit research assumptions visible. Before any
performance claims, obtain an independently sourced session manifest including
holidays, early closes and halts, plus independent vendor data receipts. These
checks do not make raw data trading-ready.

Only `RAW_UNADJUSTED` input is currently permitted; adjusted or total-return
series are rejected because the simulator does not model corporate-action
cash flows. Passing this gate is **not** a claim that a raw dataset is usable
across splits or dividends.

The replay bridge also **rejects every zero-volume daily bar**, including in
either SPY comparison leg. The underlying generic OHLCV validator correctly
permits zero-volume observations, but the simplified next-open backtester
would otherwise invent a fill on a session with no reported share turnover.
This guard applies even to a newly re-pinned snapshot: matching hashes alone
cannot make an impossible historical execution valid. It is conservative and
may exclude legitimate halted/inactive sessions. **Positive daily volume is
not proof that liquidity existed at the open**, that a simulated position
could fill, or that slippage/market impact is accurately modeled. Opening
auction, halts, quote availability and participation limits still require
separately validated intraday market data and a suitable fill model. The benchmark's fully invested SPY reference
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


## Same-session SPY risk comparisons

The comparison now carries an additional `comparison.aligned_strategy`,
`comparison.aligned_capped_spy` and
`comparison.aligned_fully_invested_spy` summary. Each reports return,
max drawdown, Sharpe and number of session observations on identical
post-warm-up dates. For non-buy-hold strategies, the legacy
`comparison.strategy.sharpe` includes warm-up flat cash sessions and must
**not** be directly compared with the shorter SPY series Sharpe. The new
aligned summaries correct that *window mismatch* without changing the raw
backtest or claiming risk/exposure-adjusted alpha. A 100%-invested SPY proxy
continues to violate the approved 10% single-instrument limit and is clearly
research context, never a compliant simulation. No live market or paper
execution is involved.


## Offline research-input fingerprint

Each `ResearchReplay` and `ResearchComparison` now exposes
`input_fingerprint_sha256`, a deterministic SHA-256 of a versioned canonical
research-input record. The record includes strategy and pinned snapshot digest(s),
as-of instant normalized to UTC, execution cost assumption, lookback,
fixed virtual equity and per-symbol research allocation, optional session
manifest digest, and **whether that manifest was separately pinned**.
Decimal scales (such as `5.0` vs `5.00`) and equivalent time-zone
representations produce the same fingerprint. Changing strategy, cost,
lookback, snapshot source revision, benchmark source, or calendar pin status
produces a different fingerprint.

This is a **reproducibility and comparison key only**, not a signature,
independent vendor attestation, trading permission, proof that the computed
result is correct, or immutable database receipt. Store the returned result,
input files and externally pinned vendor/session attestations separately.
It deliberately does not claim to include library version, Git commit,
Python environment, exchange calendar truth, corporate actions or modeling
decisions outside the helper call. A future full research provenance record
must pin code SHA, environment lock, and independent vendor source receipts.

## Critical limitations

- Next-open fills consume the same day's opening-price field, while the
  currently supplied version receipts attest subsequent full-day records
  (often at 16:00). The prior-close signal boundary does **not**
  independently prove that the opening quote itself was immutable or
  available at 09:30; a later same-day restatement could affect the
  simulated fill price. Independently pin opening-auction/quote versions
  before claiming point-in-time next-open fill correctness.
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
