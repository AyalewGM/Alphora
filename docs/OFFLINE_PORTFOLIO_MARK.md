# Reconciled offline portfolio bid marks and proposed daily-loss halt

This independent, **pure calculation** module adds a bounded risk probe toward issue #8. It is intentionally based on `main` to avoid changing the overlapping unmerged risk, reservation, monitor and LEAN draft PRs. It is not a schedule, data-feed adapter, database, execution engine, or connected paper account.

## Input contract

Call `mark_reconciled_paper_portfolio` only with a trusted, reconciled local paper-book snapshot and a one-to-one set of point-in-time, source-attested quotes. The inputs include session-opening equity, current cash, each whole-share long inventory quantity, session ID, an existing persistent halt flag and zero external deposits/withdrawals. All pending-order/partial-fill uncertainty must already have been reconciled. Reject mismatched or duplicate symbols, missing/extra/crossed quotes, nonpositive/NaN/Infinity values, untrusted or future-dated quotes, quotes more than **5 seconds** old, and quoted spreads above **1%** of midpoint. Price arithmetic uses precise Decimal and traps inexact operations.

Source attestation and `session_verified=True` are **caller assertions only**, not authenticated evidence or validation of a market calendar. A future integration must supply an independently verified exchange session/early close/halt decision (PR #32), licensed timestamped market quotes, and trust-boundary tests. The pure module does not determine whether an exchange is trading and must not be used as a trading permission.

## Financial behavior

- Compute marked equity = reconciled cash + sum(whole shares × verified **bid**). Using a bid is a conservative liquidation-side proxy, **not** an achievable sell/fill price or reliable mark in a stressed market.
- Daily P&L = marked equity − externally reconciled session-opening equity, assuming **no deposits/withdrawals or unsupported corporate cash flows** during the session. This is total observed change in account value, not a breakdown of realized/unrealized P&L.
- A breach at **−2% of session-opening equity, inclusive of the exact boundary**, or an already-persisted halt returns `halt_required=True` and an immutable `PaperAccount(halted=True)`. The caller must separately persist and independently confirm any required halt **before** another decision or synthetic order. A rally must never reset an earlier session halt.
- Invalid inputs yield `valid=False` with **no** `PaperAccount`. No permissive fallback or carry-forward of stale last prices is allowed. The separate risk gate must also independently re-check limits and durable shutdown state.
- Initial simulated account policy remains **$100,000**, planned per-trade risk **0.5%**, maximum per-instrument allocation **10%**, daily shutdown **2%**, and daily rebalance. Only the daily-loss probe is in this change. It does **not** size trades, rebalance, write the kill switch, or execute buy/sell decisions.

## Limitations and remaining acceptance gates

This uses an externally supplied account state and quote set without authenticating the feed, ensuring bid depth, modeling fees, slippage, market impact, corporate actions, unfilled orders, settlement obligations, cash interest or official opening equity. It does not handle missing delisted prices; those must fail closed for a separate corporate-action reconciliation. It does not solve the crash window between detecting a halt and writing a durable latch. The existing draft PR #23 risk guard, #30 BUY/SELL ledger, #31 quote freshness, #32 session planner and #34 durable cursor need dependency-aware integration *only after their own exact-head financial/security/QA review*. Any truthful claim of unattended paper trading also requires a real run loop, licensed data, verified exchange calendar, crash/restart replay, complete repeated trade state machine, journaled valuations and independent end-to-end tests.

Tests use exclusively fabricated quotes and balances; no historical vendor data, LEAN backtest, Alpaca endpoints or credentials, paper/real orders, network calls, deployment or automatic merges. No future live-trading authorization is implied.
