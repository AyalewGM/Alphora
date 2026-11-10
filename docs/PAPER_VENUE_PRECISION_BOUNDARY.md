# Offline paper venue equity precision boundary

**Scope:** isolated simulated `PaperBrokerAdapter.account_snapshot()` valuation only, with no network, broker SDK, orders or deployment. This focused change is independent of the stacked runner changes in PRs #43–#45 and is based on `main` at `5cec3623`.

## Problem

The venue previously inherited the calling Python `decimal` context for its marked-position total, portfolio equity, spendable cash and daily P&L. A synthetic account that bought 200 shares at $50 starts with $90,000 cash. When marked at $40.01, the correct equity is **$98,002** and P&L is **-$1,998**. Under caller precision 3, unguarded multiplication and addition can round the mark to **$98,000** and P&L to **-$2,000**. That is the approved 2% daily shutdown boundary, so the rounding error can change a risk decision.

## Change and synthetic test

A 50-digit isolated `Decimal` context with an `Inexact` trap now covers account snapshot calculations. Any inexact or unrepresentable snapshot fails closed with `INEXACT_PAPER_ACCOUNT` rather than emitting a rounded risk mark. A regression checks exact equity, P&L and available cash at caller precisions 3, 6, 28 and 80 after a deterministic simulated fill.

## Deliberate limitations

This does **not** harden every arithmetic operation in `PaperBrokerAdapter.dispatch()`, `advance()`, fill/cost-basis settlement, `OfflineReservations`, `OrderLedger` or `reconcile()`. Those need independent precision, fee and extreme-value tests. In particular, this change **does not** persist true exchange-session opening equity, enable multi-day rollover, make the in-memory paper venue crash durable, provide authentic point-in-time quotes, establish a real LEAN backtest, or implement safe unattended execution.

Keep issue #42's single-session / fail-closed containment and the multi-day financial-safety release block. No profit or execution-quality assertion follows from this narrow arithmetic fix. No PR merge, paper broker connectivity, credential acquisition or real-money trading is authorized.
