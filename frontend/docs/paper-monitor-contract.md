# Paper portfolio monitor — proposed future read-only contract

**Status: design only; no account, journal, settings-update, or execution API implemented.** The React terminal presently renders truthful **unavailable** states. Do not infer balances, account P&L, order status, or an effective policy from static defaults or the synthetic research chart.

## Existing backend endpoints actually implemented

- `GET /v1/risk/policy`: approved **static reference** risk limits (not an applied policy revision or account state).
- `GET /v1/system/capabilities`: explicit research-only capabilities with paper/live orders disabled.
- Both are validated fail-closed by `frontend/src/api.js`. Read-only GET, no cookies, no broker connectivity.

## Proposed authenticated read-only data model (NOT an existing endpoint)

A separate Backend/Risk PR must supply a server-authoritative, authenticated, tenant/user-scoped **paper account snapshot**. The frontend must never calculate its own account authority from synthetic prices. Snapshot should contain:

| Field / class | Required meaning |
| --- | --- |
| `schema`, `snapshot_id`, `revision`, `as_of` | Typed version, monotonic revision, unique ID and timezone-aware timestamp |
| `session_id`, `session_open_equity_usd` | Verified exchange-session identity and persisted opening equity used for the daily-loss halt |
| `cash_usd`, `reserved_cash_usd`, `available_cash_usd` | Decimal strings; settled cash and pending reservations reconciled in backend |
| `equity_usd`, `realized_pnl_usd`, `unrealized_pnl_usd`, `day_pnl_usd` | Exact audited amounts; mark provenance and freshness attached |
| `positions`, `pending` | Server-attested records; symbol, integer shares, verified mark/time and exposure |
| `halted`, `halt_reason`, `halt_as_of` | Durable server-side kill-switch state; **unknown is not equivalent to false** |
| `reconciliation_status`, `reconciliation_as_of`, `reconciliation_receipt` | Independently verifiable current reconciliation; stale/WARN blocks new entries as required |
| `effective_policy_revision`, `effective_policy_receipt`, `effective_at_session` | Transactionally persisted **effective** policy, distinct from a draft proposal receipt |

The frontend must show **UNAVAILABLE / UNKNOWN / NOT VERIFIED** if any required field, source signature, scope, identity, timestamp, reconciliation or revision check fails; never fall back to synthetic numbers. The app must distinguish a valid read-only backend connection from verified account-state data.

The paper decision journal must be independently authenticated and paginated with stable cursors, intent/version IDs, server timestamps, proposal rationale, risk and reconciliation verdicts, transitions and receipts. Missing journal is an empty **unverified** state, not proof that no trades occurred. The UI should render read-only entries with no submit or mutation actions.

## Settings proposal compatibility

The draft form renders the six-key, exact-decimal string `paper-risk-proposal-v1` wire object of **merged PR #66** (subject to the separate proposed constructor correction in draft PR #68). The values are **not sent or saved**. The form deliberately limits percentage input to four decimal places, rather than exposing the full extreme-precision backend Decimal domain. Validation errors identify and focus affected fields; a successful preview does not attest a portfolio, active policy revision, or released halt. The current hard ceilings are 0.5% planned per-trade risk, 10% instrument exposure, and a 2% daily-loss halt; tighter proposals are permitted. New starting equity applies only to a **new** simulated portfolio, never automatically to an existing book. Weekly/manual cadence is a proposal, not a functional scheduler. Neither the form nor its validation may clear an active halt, authorize any execution mode or weaken a safety gate.

Any future settings-write functionality requires independently reviewed authenticated optimistic revision checks, auditable operator action, explicit confirm, atomic effective-session activation, persistence and rollback protection; a draft `to_wire()` or receipt is **not** an effective policy revision.

## Backend and review handoff

Before wiring the frontend to additional backend reads, Backend/Risk must finalize #42 multi-session opening-equity and restart-safe halt, immutable session/reconciliation provenance, read-only auth scheme and stable schema. Independent Financial QA verifies precision/ledger invariants, stale marks, crash/restart, source authenticity and negative tests. Independent Security verifies principal/scoping, replay/caching and mutation boundary. Frontend reviews keyboard/mobile/readability and all missing/partial snapshot scenarios.

**No Alpaca connection, broker credentials, order endpoints, deployment, auto-merge or real-money trading.**


## Read-only empty-state table presentation

The dashboard now reserves accessible, horizontally scrollable **positions/pending exposure** and **decision journal** tables. Header columns are present so operators can understand the future evidence dimensions, but their only row explicitly states **UNAVAILABLE — NOT ZERO POSITIONS** or **NOT VERIFIED — NOT AN EMPTY JOURNAL**. These are not mock portfolios, zero-balance claims, order execution histories or evidence of a clear halt. A keyboard-focusable scrolling region makes all headers reachable on narrow screens. No new GETs, persistence, credentials or trade controls were introduced.

Before replacing those rows with actual records, Backend and independent QA must agree on authenticated scoped snapshots, exact decimals, pending order reservations, revision/cursor continuity, session and timestamp/mark provenance, freshness, reconciled state and fail-closed handling of partial or stale evidence. Static source-contract tests are not a substitute for browser/mobile/screen-reader validation.
