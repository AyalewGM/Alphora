# Offline paper venue: cent-exact pending cash reservations

## Defect

In `PaperBrokerAdapter.dispatch`, BUY order reservations previously used the ambient Python `decimal` precision. With $100,000 simulated cash and precision 3, a 1-share BUY at $100,000.01 could be rounded to a $100,000 hold and incorrectly accepted. Two separate $49,999.99 BUY holds could also be rounded to $100,000 reserved instead of the correct **$99,999.98**. Unwinding under a low-precision context could likewise lose cents.

These are deterministic synthetic inputs, **not** actual quotations or trading instructions.

## Change

- Compute the proposed BUY hold, current available cash, and next reserved-cash amount inside a pinned 50-digit Decimal context with the `Inexact` trap, **before any venue order mutation**.
- Reject BUYs exceeding actual available cash with the existing `INSUFFICIENT_CASH` outcome; fail closed with `INEXACT_PAPER_RESERVATION` if a proposed hold cannot be represented exactly.
- Release the identical hold using isolated arithmetic on cancel, expiry or simulated fill, rejecting inexact arithmetic and negative reserved balances.
- Synthetic regression tests cover precision 3, 6, 28 and 80 and verify order acceptance, precise holds, releases, cash invariance and a valid retry after rejection.

## Explicit boundaries

This is stacked on draft PR #46 (which fixes *read-only* venue account snapshot arithmetic) and is independent of runner containment PRs #43–45. It **does not** address `advance()` slippage/fill/position cost arithmetic, multi-order atomic fill, durable cross-process OMS recovery, session-opening-equity persistence, exchange-calendar attestation or authentic vendor marks. Multi-day 2% daily-loss enforcement is still blocked in #42. Cash-only paper simulation remains disconnected and **no** Alpaca credentials, broker API connection, paper/live orders, deployment, PR merges or profitability claims are authorized.

Evidence: the test-only PR head `f24b5ea` is intentionally failing; subsequent GitHub Actions runs and independent Devin/Muse exact-head review must be recorded before merge consideration.
