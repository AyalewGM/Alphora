# Offline virtual cash-reservation ledger (Issue #8, phase 2)

This is **purely local hypothetical paper accounting**. It is not an exchange order, broker OMS, trading engine, data feed, or authorization for Alpaca or live/paper brokerage connectivity. It does not submit, route, or receive any orders; "fills" are explicitly **synthetic receipts created by a local test or simulator**.

## Scope and invariants

- Initialize a local on-disk SQLite database with exactly **$100,000 virtual cash**, bound to a unique session ID. The original risk policy remains 0.5% planned per-trade stop risk, 10% instrument allocation, 2% session-opening-equity daily loss halt, cash-only long-only, and daily rebalancing (not yet implemented here).
- \`reserve_buy\` requires a caller-asserted reconciled \`PaperAccount\`, an existing \`BuyProposal\`, a unique idempotency key, and a pre-budgeted maximum synthetic fee. It atomically rechecks \`validate_buy\` and available cash **after accounting for all pending reservations**. Same-symbol exposure includes the larger of caller-reported marked position value and internal cost basis, plus pending reserves. Both fee budgets and full buy-limit-price notional reserve cash.
- The single-session simulated opening-equity baseline is fixed at **$100,000**. A caller cannot change `session_open_equity` to artificially change the 2% daily-loss threshold or risk caps. The separate `equity` and `day_pnl` fields are still caller assertions; authenticated portfolio marks remain a future requirement.
- Opening an existing database now verifies its internal cash/receipt invariants before any additional mutation. An interrupted/failed synthetic fill rolls back as one SQLite transaction; this is not a substitute for process-kill, filesystem and power-loss testing.
- \`record_hypothetical_fill\` only accepts whole-share synthetic BUY fills at or below their stored limit and strictly above stored stop; quantity and fees cannot exceed pending limits or budgets. It atomically updates simulated cash, cost-basis shares, and remaining reservation. Exact duplicate fill IDs are no-ops; conflicting duplicates are rejected.
- \`cancel_remaining\` only releases unfilled pending virtual cash and retains filled shares. \`halt\` permanently locks new reservations and new synthetic fill mutations for that session, including after restart; cancellation remains available. The database uses WAL, FULL synchronous commits and immediate write transactions. Simple corruption and missing-book metadata fail closed; this is **not cryptographic tamper evidence**.
- Accepts only a persisted file path, not \`:memory:\`, empty or SQLite \`file:\` URIs. The file must remain local and secured by the operator.

## Boundaries — must NOT be bypassed

The external \`reconciled=True\` flag and \`PaperAccount\` equity/P&L are **assertions, not independently authenticated facts**. This module does not verify market quotes, position marks, vendor/provenance timestamps, sessions, or exchange conditions. Filled holdings use **cost basis**, not current market value; caller-supplied marked exposure must come from a separately reviewed reconciliation system. The 2% loss halt cannot be considered real-time market protection until portfolio marks and daily equity are independently verified. Stop prices do not guarantee stop fills or loss limits.

**Not implemented:** sell side, inventory disposal, split/dividend/delisting adjustments, session rollover, settlement constraints, actual commissions/slippage modeling, true OMS order state, robust external tamper anchors/backups, broker reconciliation, feed-disconnect fail-safe, approvals, or automated execution. A synthetic fill is not evidence that any actual venue would fill a limit order. The full platform is **not** ready for paper connectivity, let alone real trading.

The independent phase-1 decision journal in draft PR #13 is **not** replaced or modified; eventual integration must be reviewed against event ordering, durable cross-table consistency, and replay.

## Verification and handoff

Run \`python -m unittest discover -s tests -v\`. Synthetic tests cover concurrent reservations across SQLite connections, cash/instrument/risk boundaries, partial and duplicate fills, stale cash snapshots, cancellation, one-way halts, restart durability, corrupted ledgers, fixed opening-equity baseline, rejected corrupt reopen, injected SQL transaction failure and session mismatch.

Next owners: financial correctness review of exposure and cash arithmetic; independent SQLite/recovery QA including actual process crash/injected I/O failures; separate data/reconciliation service design; human security/governance review before integration. **Do not merge, deploy or connect credentials/orders** on the basis of these synthetic tests alone.
