# Broker adapter layer — design and phase-1 order ledger

This is the design for a **paper-only** order-execution layer plus its later
(separately authorized) live extension. Nothing in this layer is an exchange
order, broker OMS, trading engine, market data feed, or authorization for
Alpaca or any other brokerage connectivity. The draft contains a local-only venue, lifecycle ledger, manager and session
runner. They require merged-accounting regression acceptance before integration.

## Authorization boundary (must NOT be bypassed)

`tests/test_broker_boundary.py` AST-scans every Python file and fails on broker
SDK imports, execution-method names (`submit_order`, `place_order`,
`cancel_order`, `create_order`, `close_position`, … — including via `getattr`,
dynamic import, or string literal), broker hostnames, broker credential
environment names, and broker packages in `pyproject.toml`. This is a
defense-in-depth source tripwire, not a substitute for code review or runtime
network isolation.

Consequences, by design:

- Adapter method names avoid the banned vocabulary entirely (`dispatch`,
  `cancel`, `order_status`, `account_snapshot` — never `submit_order`,
  `cancel_order`, `place_order`, `create_order`).
- A live adapter **cannot be added** without a deliberate, reviewable edit to
  the tripwire test exempting one designated file. That edit is the auditable
  authorization event for brokerage connectivity, matching the test's own
  note: "Brokerage connectivity requires separate authorization."
- `validate_buy` rejects `mode != "paper"`; extending it is a second
  deliberate, reviewable change required before any live dispatch.

## Architecture

```
EntryPlan / ExitPlan (decision.py — read-only proposals, intent keys)
  -> OfflineReservations.reserve_buy / record_hypothetical_sell
  -> OrderLedger (orders.py — durable lifecycle record)         [IMPLEMENTED]
  -> risk.validate_buy re-check immediately before dispatch (TOCTOU guard)
  -> manual KillSwitch guard (no automatic broker access)       [DRAFT]
  -> offline BrokerAdapter.dispatch                             [DRAFT]
  -> fills recorded -> reservation settled or released
  -> Reconcile internal book/ledger and simulated venue         [DRAFT]
```

Planned modules: `alphora/broker/base.py` (protocol + types)
**[implemented]**, `alphora/broker/paper.py` (deterministic offline matching
engine) **[implemented]**, `alphora/broker/manager.py` (orchestration)
**[implemented]**, `alphora/reconcile.py` [planned], `alphora/killswitch.py` [planned].

## Order ledger (alphora/orders.py) — implemented

- SQLite, durable local file path only (no `:memory:`, no `file:` URIs), WAL,
  `synchronous=FULL`, `BEGIN IMMEDIATE` writes — same posture as
  `OfflineReservations`. Bound to one `YYYY-MM-DD` session at creation.
- `orders` rows are created only with `mode="paper"`, `LIMIT` type, `DAY`
  time-in-force, whole shares, and a 64-hex content-derived `client_order_id`
  (the sha256 idempotency key convention already used by `decision.py`
  intent keys). Same-key same-fields retry is a no-op; same-key different
  fields raises `CONFLICTING_ORDER_ID`.
- State machine: `CREATED -> SENT -> ACKED -> PARTIALLY_FILLED -> FILLED`;
  `ACKED`/`PARTIALLY_FILLED` -> `CANCEL_PENDING -> CANCELLED`, or `REJECTED` /
  `EXPIRED`. `CREATED` may cancel locally (`CANCELLED`) or be locally rejected
  (`REJECTED`). Terminal statuses are immutable; transitions only move forward.
- `SENT` is the **lost-acknowledgment state**: the ledger claims the intent was
  handed to an adapter, but no ack is recorded. A `SENT` order can only leave
  via `ACKED`, `REJECTED`, or `CANCELLED` — and the last of those requires
  `confirmed_absent=True`, a caller assertion that reconciliation proved the
  venue never received it. SENT orders block nothing inside this module but
  are surfaced by `pending_reconcile()`; the future manager must refuse new
  dispatches while any exist unresolved.
- `order_events` is an append-only, hash-chained audit log (same
  canonical-JSON + sha256 chain construction as `journal.py`), verified on
  open and before every mutation together with structural checks on orders,
  fills, and meta. Corruption fails closed as `LEDGER_STATE_CORRUPT`; this is
  casual-tamper detection, **not** a cryptographic trust anchor.
- `order_fills` records claimed fills: whole shares, cumulative quantity never
  exceeds the order, buy fills priced at or below limit / sell fills at or
  above limit, non-negative fees. Fill IDs dedupe semantically; conflicting
  reuse raises `CONFLICTING_FILL_ID`. Fills may arrive during `CANCEL_PENDING`
  (cancel/fill race); completion from that state lands in `FILLED`.

**Explicitly not done here:** the ledger performs no cash accounting (that is
`OfflineReservations`' job — atomic coupling of cash release and order state
is a manager milestone), no risk decisions (`validate_buy` is re-run by the
caller just before dispatch), no venue contact, no quote validation, and no
session rollover. `"SENT"`/`"ACKED"`/`"FILLED"` are records of caller or
adapter assertions, not verified venue facts.

## Paper venue (alphora/broker/base.py + paper.py) — implemented

- `base.py` defines the venue-facing contract (`BrokerAdapter` protocol):
  `account_snapshot`, `positions`, `open_orders`, `order_status` are read-only;
  `dispatch` and `cancel` are the only calls that change venue exposure. Venue
  vocabulary (`ACCEPTED`/`FILLED`/`CANCELLED`/`REJECTED`/`EXPIRED`) is distinct
  from ledger-internal states; the manager maps between them.
- `PaperBrokerAdapter` is a fully offline, deterministic venue: identical call
  sequences give identical results, broker ids are sequential
  (`paper-00000001`, …), and time is caller-supplied.
- Marks are **caller-supplied single asserted-executable prices** per symbol.
  A BUY fills completely iff `mark*(1+slippage) <= limit`, at the adjusted
  price; a SELL iff `mark*(1-slippage) >= limit`. Optional `slippage_bps` and
  `fee_per_order` are constructor-configured. There is no spread, queue
  position, latency, or partial-fill model — fills are a simplification, not
  evidence, of real venue behaviour.
- Cash/share accounting mirrors the reservations posture: a BUY dispatch
  reserves `qty*limit + fee` against asserted cash; a fill debits actual
  `qty*price + fee`; cancel/session-expiry releases the hold. A SELL reserves
  shares (long-only: dispatch rejects beyond unreserved holdings) and credits
  `qty*price - fee`, reducing cost basis at average cost. `account_snapshot`
  rejects any held position missing a mark from the latest advance. Never
  assume cost basis or previous-session price is a current valuation.
- Dispatch dedupes by `client_order_id` (identical retry returns the same
  ack; conflicting reuse is rejected) — matching the ledger's idempotency-key
  convention so the manager can retry safely across crashes.
- **Explicitly not modelled:** partial fills, market/stop order types,
  extended-hours, borrow/locate, venue reject codes beyond
  `INSUFFICIENT_CASH`/`INSUFFICIENT_SHARES`/`CLIENT_ORDER_ID_CONFLICT`,
  and any latency between dispatch and acknowledgment (the paper venue acks
  synchronously — the `SENT` lost-ack path is exercised only by ledger tests
  and by a future fault-injecting adapter wrapper).

## Order manager (alphora/broker/manager.py) — implemented

`PaperOrderManager` is the only component that touches all three stores. It
never originates a decision and keeps the risk gate physically in the order
path:

- `execute_entry(plan, account, *, fee_budget, now, reconciled)`:
  `book.reserve_buy` (atomic cash hold + internal risk recheck, idempotent by
  intent key) → `ledger.create` → `validate_buy` re-run against cash net of
  *other* reservations (TOCTOU guard; the order's own hold never counts
  against itself, including on retry) → `mark_sent` → `adapter.dispatch` →
  `mark_acked`. A venue rejection lands `REJECTED` and releases the cash
  reservation; a local veto or halt never reaches the venue.
- `execute_exit(plan, *, symbol, limit_price, fee_budget, now)`: new
  checks reconciled long inventory, excludes any other working SELL quantity,
  then dispatches without creating a competing cash/position ledger. The
  merged #30 `record_hypothetical_sell` is the **sole** authoritative cash,
  basis and position update on a SELL fill. A daily-loss book halt permits a
  verified reducing SELL, while a manual kill file blocks all dispatch.
  The bounded #30 book refuses new BUY reservations after its first SELL;
  this prototype is not yet a full multi-session OMS.
- `sync(now)` is the crash-recovery path and is safe to re-run: `SENT`
  orders are resolved via `order_by_client` — for the paper venue, absence is
  confirmed absence (`CANCELLED` + reservation released); fills apply to
  ledger then book (each store dedupes by fill id, so a crash between them
  heals on the next sync); venue `CANCELLED`/`REJECTED`/`EXPIRED` map to
  terminal ledger states and release reservations. A venue `FILLED` that the
  recorded fills cannot reproduce raises `VENUE_LEDGER_DIVERGENCE` — loud,
  not silently repaired.
- `cancel(client_order_id)`, `advance(marks, at)`, `expire_session(at)`
  round out the flow.
- Known limitation by design: ledger and book are separate SQLite files, so
  fill settlement is not one transaction; ordering (ledger first) plus
  idempotent replay makes crashes recoverable, and anything beyond that is
  reconciliation's job.

## Reconciliation and kill switch — implemented

`alphora/reconcile.py` diffs book/ledger vs asserted venue truth into a
`Reconciliation` certificate (`status`, `session_id`, `checked_at`, `diffs`):

- **Expected-state model.** The venue is compared not against raw book state
  but against book state *plus* fills the venue asserts but the ledger has not
  yet applied. Venue-ahead fills therefore produce WARN settlement diffs
  (`UNSETTLED_FILLS`, `POSITION_SETTLING`, `CASH_DRIFT`), never FATAL —
  anything else is genuine divergence.
- **FATAL** (operator required, blocks dispatch): `POSITION_MISMATCH`,
  `CASH_MISMATCH`, `VENUE_ORDER_UNKNOWN`, `ORDER_FIELD_MISMATCH`,
  `LEDGER_ORDER_MISSING`, `STALE_VENUE_ORDER`, `LEDGER_FILL_UNKNOWN`,
  `RESERVED_MISMATCH` — the last only when the *venue* holds reserve that
  explained holds cannot account for; book over-reserving is
  `RESERVED_DRIFT` (WARN), never silently tolerated.
- **WARN** (explainable in-flight state, heals via sync/cancel/retry):
  `UNDELIVERED_INTENT`, `LOST_ACK`, `CANCEL_IN_FLIGHT`, `CANCEL_SETTLING`,
  `UNSETTLED_*`, `UNSETTLED_FILLS`, `POSITION_SETTLING`, `CASH_DRIFT`,
  `UNLINKED_RESERVATION`.
- **Dispatch gating** (`manager._require_clean_recon`): execute paths require
  a certificate bound to the session, not future-dated, fresh within
  `max_recon_age_seconds` (default/cap 120s). `FATAL` blocks everything;
  `LOST_ACK` blocks new dispatch too (an unresolved SENT order must be
  resolved through `sync()` first, which needs no certificate). Other WARNs
  do not block — blocking them would deadlock their own remedies.
- **`alphora/killswitch.py`** layers: a **halt file** (presence = halted,
  checked live before every dispatch — absence grants nothing), the book's
  one-way session halt, and `kill(reason)` which writes the file (first
  reason wins — later kills don't overwrite it), halts the book, and
  best-effort cancels every open venue order, collecting per-order errors.
  Cancel-only scope: no forced liquidation path exists. `resume()` removes
  the file only; a book halt persists for the session by design.
- The manager takes `killswitch=` at construction and checks it before
  reserving *and* immediately before `adapter.dispatch` — the file may appear
  mid-flight.

## Session runner (alphora/runner.py) — implemented

`PaperSessionRunner` drives the whole pipeline over caller-supplied
`SessionDay`s (mark timestamp, per-symbol `Bar`s, per-symbol `SessionSignal`s):

per day — `advance` (prior working orders meet the mark) → `sync` → account
marked to marks → `reconcile` → `propose_exit`/`execute_exit` per position →
**exit fills settle** → `observe_daily_loss` arms the one-way book halt →
`propose_entry`/`execute_entry` for WATCHLIST/VERIFIED signals → entries fill
at the same mark → `expire_session` → `DayReport` (equity, cash, actions,
recon status, halted). Ordering matters: liquidation settles before the halt
arms, and new intents always face the armed halt.

Marks are *asserted-executable* prices — the caller controls which bar field
was tradable and must not feed marks unknowable at `mark_time`; the runner
cannot detect lookahead bias in its inputs. Scanner/catalyst signals are
caller-supplied for the same reason (vendor-attested timestamps are required
upstream). `RunReport` summarizes final equity and halt state.

## Live path — deferred, separate authorization

When paper forward-testing justifies it: one designated tripwire-exempted
file (e.g. `alphora/live/alpaca.py`) or a separate private package;
double-gated construction (explicit `enable_live=True` plus an environment
flag); credentials from environment/secrets manager, never committed.
Alpaca's paper endpoint is the intended intermediate step: real API mechanics
(acks, partial fills, rejects, cancels) with no money at risk.

## Milestones

| # | Deliverable | Status |
|---|---|---|
| M1 | `orders.py` ledger + state machine + tests | implemented |
| M2 | `broker/base.py` protocol + `broker/paper.py` matching engine | implemented |
| M3 | `broker/manager.py` wiring + sell-path cash accounting + crash recovery | implemented |
| M4 | `reconcile.py` + `killswitch.py` | implemented |
| M5 | Offline session runner; forward paper run | implemented |
| M6 | Boundary exemption + Alpaca **paper** adapter | separate authorization |
| M7 | Alpaca live adapter, minimal size | separate authorization |

## Verification

`python -m unittest discover -s tests -v`. `tests/test_orders.py` covers the
full happy-path lifecycle, illegal and terminal transitions, idempotent
retries and conflicting reuse, cancel/fill races, lost-ack resolution,
fill bounds, restart persistence, event-chain and row corruption fail-closed
detection, uncommitted-transaction rollback via abrupt child exit, and
serialized concurrent creation. `tests/test_manager.py` covers the
end-to-end entry/exit flow with book/ledger/venue consistency, idempotent
retries through dispatch, venue-reject and local-veto release paths,
lost-ack recovery and confirmed-absent resolution, restart persistence,
cancel and session expiry settlement, and sync idempotency; the sell-side
book accounting is covered by `SellReservationTests` in
`tests/test_reservations.py`. `tests/test_reconcile.py` covers the
severity model end-to-end (settlement-lag WARNs healing via sync,
unknown-order/position/cash-divergence FATALs, certificate validation);
`tests/test_killswitch.py` covers halt-file and book-halt blocking,
cancel-all with collected errors, first-reason persistence, and resume
semantics. `tests/test_paper_adapter.py` covers dispatch
validation and client-id dedupe, buying-power and share-availability
rejection boundaries, limit/slippage/fee fill behaviour, reservation release
on fill/cancel/expiry, position basis and equity marking, deterministic
broker ids and fill ordering, and the absence of execution-vocabulary
attributes.

## Integration correction — 2026-11-08

This draft merges latest `main` while preserving #30's unified BUY/SELL
bookkeeping. The former alternative sell reservation code was discarded.
No independent live broker is present or authorized. Cent rounding policy:
Decimal `ROUND_HALF_EVEN` at `0.01` for venue-executed prices, simulated
cash changes and proportional average-cost retirement; a full sell retires
remaining cost basis to avoid stranded subcent residues. Venue fees and
order limit prices must be whole cents, and incoming synthetic marks are
normalized to cents before use. Receipt prices and cash are never allowed
to retain fractional cents.

Current-day holdings require a fresh mark from the most recent advance,
and runner days missing any held symbol are rejected before ledger writes.
No source/quote authentication, vendor timestamps, market feed or exchange
calendar is established by this offline module. Do not treat a stale-price
error as evidence that a 2% halt was correctly evaluated; it is an unknown
valuation requiring no new BUYs and operator reconciliation.

**Halt decisions:** loss-triggered book halt blocks new buys but may permit
independently reconciled reduce-only long exits with fresh marks; a manual
halt file blocks all dispatch until explicit operator reset. Neither
condition authorizes real-money trading.

### Reduce-only SELL provenance gate

The manager requires a `CLEAN` reconciliation certificate and a ticker mark
from the paper venue's latest simulated batch, no more than five seconds old
relative to the decision time. A missing or stale mark raises
`UNVERIFIED_OR_STALE_MARK` and no new SELL is dispatched. This is a *freshness
contract for caller-attested synthetic data*, not a vendor authentication or
authorization to connect a live market feed. A manual file kill takes
precedence over the loss-halt allowance.
