# Durable local monitor completion cursor — offline research only

This PR adds an isolated SQLite store for the **due-work planner** introduced
in [draft #32](https://github.com/AyalewGM/Alphora/pull/32). It does not add
a timer, price feed, account valuation, risk kill switch, broker adapter,
Alpaca connectivity, external network access, orders, or fills.

## Operator-supplied completion acknowledgments

A future *trusted local supervisor*, not this module, must independently
verify US exchange calendar / early closes / halts and licensed timestamped
prices, evaluate and reconcile portfolio risk, perform due tasks, and then
call `record_completed(...)` **only after** successful work. The function:

- Requires `VerifiedSession` and the current exact `MonitorCheckpoint`
  obtained from `load(day)`; validates that the approved session
  (premarket 04:00–09:30 ET, regular 09:30–16:00 ET or early close) is active
- Acknowledges a nonempty set of completed due tasks: `positions`,
  `watchlist`, `discovery`, `news`
- Uses SQLite `BEGIN IMMEDIATE` and an exact expected-state compare-and-swap
  to reject stale concurrent acknowledgments, instead of crediting unrun work
- Persists canonical aware UTC timestamps; fail-closed on corrupt,
  noncanonical, naive, out-of-session or future values; treats a new session
  date as a separate due-work cursor without modifying prior records
- Updates **only explicitly reported successful tasks**; failure/exception
  rolls back the entire acknowledgment without prematurely skipping work

Target intervals remain the approved design: held positions / risk every 5s,
watchlist 15s, discovery 60s, news 300s. Separately, risk checks MUST also
run after each market update and synthetic fill; they cannot be governed
solely by these periodic cursors.

## Critical limitations

1. **Completion acknowledgement is caller-attested, not verified.** The
   module cannot prove a market read, news evaluation or actual risk
   computation occurred. A caller who acknowledges early can wrongly suppress
   later work. Integrate only with audited result journaling after independent
   review.
2. An atomic SQLite cursor alone cannot provide exactly-once execution of
   future external work. A crash after processing but before acknowledgment
   will schedule work again; design operations as idempotent and reconcile
   receipts at restart.
3. The SQLite file must live outside the Git repository in a trusted,
   access-controlled local directory; this module does not provide encryption,
   authenticated provenance, anti-tamper protection or cross-process
   distributed locking beyond SQLite's single-file transaction guarantees.
4. This store does **not** persist equity, actual positions, pending orders,
   daily-loss kill switches or immutable audit logs. Those require the
   independent OMS ledger and risk state. It does **not** permit simulated
   premarket execution. No after-hours monitoring/trading.
5. The calendar's `calendar_verified=True` flag is an externally asserted
   claim, not proof of exchange dates. Feed/calendar integration remains
   unimplemented.

## Acceptance dependencies

- This draft stacks on #32 → #30 → #23; acceptance also requires
  exact-head combined CI and independent human financial/QA/security review.
- Before unattended operation: independently verified calendar and lawful
  licensed market feed, quote authentication/freshness, persistent 2% loss
  halt integrated with reconciled positions, a restart-safe signal-to-intent
  journal, complete interleaved BUY/SELL/partial-fill ledger, and observed
  crash/reconnect recovery tests.
- Approved starting policy unchanged: **$100,000 virtual equity**, planned
  risk **0.5% per trade**, maximum **10% per instrument**, hard **2% daily
  drawdown halt** and **daily rebalancing**.

No connection to Alpaca, credentials, broker orders, actual LEAN run,
deployment, merge, or real-money trading is authorized.
