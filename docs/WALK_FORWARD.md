# Offline purged walk-forward fold schedule (research only)

This is a **date-index partitioner**, not an ML model, backtest, market-data
validator, or execution component. It addresses one specific source of
lookahead bias in later ML/FinRL experiments: labels with a forward return
horizon must not rely on prices inside the test interval.

## Usage

```python
from datetime import date
from alphora.research.walk_forward import rolling_purged_folds

# EXAMPLE ONLY: substitute dates from independently validated, pinned,
# complete historical exchange sessions (not an unverified date generator).
session_days = tuple(date.fromisoformat(s) for s in validated_day_strings)

folds = rolling_purged_folds(
    session_days,
    train_sessions=252,
    test_sessions=21,
    label_horizon_sessions=5,
    embargo_sessions=2,
    holdout_sessions=63,  # Reserve the last 63 sessions for untouched final evaluation.
)

for f in folds:
    train_days = session_days[f.train_start:f.train_stop]
    test_days = session_days[f.test_start:f.test_stop]
    # Fit normalizers, imputers, selectors and models *only on train rows*.
    # Evaluate on test rows after the labels have actually matured.
```

## Final untouched holdout boundary

Set `holdout_sessions=N` to reserve the last N supplied session rows for a
separately controlled final evaluation. The research fold generator rejects
configurations without enough **mature pre-holdout labels** and requires every
training and test label index to finish strictly before
`len(session_days) - holdout_sessions`. Each emitted fold records that exclusive
boundary as `holdout_start_index`. Its indices are reproducible, but neither
the dates nor the labels are source-authenticated. The function **never fits,
predicts on, or scores the reserved tail**, so a separate evaluation pipeline
must enforce holdout release and one-time evaluation controls.

This prevents an otherwise subtle leakage path: a research test observation
may be outside the reserved tail while its forward return label would use
outcomes *inside* the supposed untouched holdout. Keep the external holdout
manifest pinned and independent, and avoid selecting hyperparameters using
the final holdout. The default `holdout_sessions=0` preserves the prior
research schedule.

All index windows are half-open. The train window is **fixed-length rolling**,
not expanding. Each test window has exactly `test_sessions` distinct dates,
and the next one begins immediately after the previous test window.
There is no partial last fold; report the number of unevaluated trailing
rows rather than silently claiming 100% coverage.

With `label_horizon_sessions=h`, a sample at index `i` uses a label
dependent on `i+h`. The last training label index is
`train_stop - 1 + h`. The first test sample index is
`train_stop + h + embargo_sessions`, so every training label finishes
strictly before the test period and the embargo separates the two. The last
test label must also mature **within the supplied history**. This avoids
optimistic results from scoring future-unobserved outcomes.

## Reproducibility and limitations

- This function works only with **caller-supplied dates**. It verifies order
  and types, but not that they are a complete NYSE/Nasdaq calendar. It does
  not validate vendor arrival/revision timestamps, price adjustments,
  delistings, ticker changes, cash flows, corporate actions or licensing.
- Before use with actual market data, obtain independent, licensed
  point-in-time vendor receipts, then validate and externally pin the dataset
  using the existing `data_provenance` snapshot contracts and PR #17's
  proposed replay gate (if independently reviewed and merged).
- A purged fold alone **does not prevent feature lookahead**. Fit transforms
  exclusively on each fold's training records, use source-available timestamps
  for fundamentals/news, handle overlapping instruments/survivorship, and
  verify label computation, market holidays and release delays.
- **Do not optimize or select a model on the final out-of-sample segment.**
  Use a separately held-out final evaluation, nested tuning when appropriate,
  documented random seeds, package/environment pins, model hashes and
  transaction-cost assumptions. Fold-to-fold test periods are non-overlapping
  but adjacent outcomes and model retraining can still be statistically
  dependent; do not interpret sample count as independent trials.
- There is no numerical accuracy, P&L, return, Sharpe or profitability claim,
  LEAN engine run, data feed, broker integration, order, deployment, or
  automated trading. The fixed research schedule is not an actual account
  rebalance instruction.

## Test command and acceptance

`python -m unittest discover -s tests -v`.

Unit tests use synthetic weekday sequences and verify: fixed rolling windows,
non-overlapping OOS dates, label maturity and strict purge/embargo boundaries,
insufficient dataset and reserved-holdout rejection, malformed and duplicate date rejection,
invalid parameter rejection and deterministic output.

Before an ML or FinRL milestone is accepted, independent financial/ML QA
must demonstrate a complete source-available, point-in-time feature pipeline
and true out-of-sample evaluation, compare against aligned SPY and simple
baselines, and report realistic costs, drawdowns and uncertainty.

**Risk governance:** Alphora remains offline and personal paper-only.
The existing $100,000 simulated starting equity, 0.5% planned per-trade risk,
10% allocation cap, 2% daily loss halt and daily rebalance settings are
unchanged. This splitter enforces *none* of those portfolio controls.
No Alpaca credentials, connections or orders are permitted.
