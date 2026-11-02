# Alphora PR #3 — Professional engine evaluation (research-only spike)

## Why a separate spike?
PR #2 provides a simple single-asset simulator on a draft branch; it is **not** a professional-grade simulator. We deliberately keep the LEAN evaluation independent of merging PR #2.

This PR introduces a **non-trading example** in `research/lean_spy_baseline/main.py` and documents how to obtain a reproducible local run. It does not invoke LEAN in CI, connect Alpaca, fetch data, or generate any performance claim.

## LEAN setup on your workstation
The official LEAN CLI requires Docker, a QuantConnect account and—per current official documentation—membership in a paid-tier organization for local CLI backtests. Data are a separate dependency/cost. Keep credentials in local config, **never in Git**.

1. Install Docker and Python; create a separate virtual environment.
2. Install `lean` CLI; log in locally using `lean login`.
3. Run `lean init` in a **separate local workspace**, not the tracked repository.
4. Copy `research/lean_spy_baseline/main.py` into a LEAN project directory as `main.py`.
5. Obtain legitimately licensed US equities data for the period; verify coverage, normalization, corporate actions and timezone.
6. Run `lean backtest "project-directory" --output "separate-output-directory"`.
7. Store the engine image digest, CLI version, algorithm SHA, data vendor/version, inputs, start/end and backtest output hash in a local experiment record.

Official docs: https://www.quantconnect.com/docs/v2/lean-cli/api-reference/lean-backtest

## Acceptance gates before adoption
- Confirm a verified **actual LEAN run** on licensed data; compare fills, split/dividend accounting, timezone, valuation, fees and slippage with expected fixtures.
- Establish two benchmarks: (A) fully invested SPY total-return, (B) constrained 10%-SPY/90%-cash for like-for-like exposure comparisons.
- Evaluate LEAN versus NautilusTrader on dataset availability, testability, total operating cost, and behavior under restarts.
- Separate model selection, point-in-time data validation and out-of-sample evaluation.
- Maintain a hard process boundary: offline research commands may **only** invoke `lean backtest`; never `lean live`, brokerage APIs, or credential-driven mode switches.

## Alpaca integration — later work
An existing Alpaca account is useful for future paper execution. A future adapter will accept **paper credentials only**, check that the trading base URL is `https://paper-api.alpaca.markets`, and reject live endpoints. Do not place keys in issues, chat, logs, CI or source.

## Limitations
This is scaffolding and integration documentation, **not a production LEAN implementation**. The example has no real profit results. It does not enforce the 0.5% per-trade stop-risk or 2% daily halt; it must never be reused to submit broker orders.
