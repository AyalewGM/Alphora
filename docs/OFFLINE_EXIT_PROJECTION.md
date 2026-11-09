# Offline synthetic SELL receipt projection (issue #8)

This is a standalone **read-only reference calculation**, not an OMS, order queue, paper or live brokerage adapter, portfolio reconciliation system, or release gate. No credentials, network, actual market fills, or deployment are introduced. The existing buy-only SQLite reservation ledger is unchanged.

The module accepts a caller-asserted starting cash balance and fee-inclusive aggregate whole-share acquisition basis for a single dated session. The snapshot must be a balanced **$100,000 initial simulated book**, with all pending BUY reservations resolved, no prior SELL proceeds and no realized P&L. No deposits, withdrawals, corporate actions, tax-lot accounting, margin, or settlement restrictions are modeled. The module cannot verify opening positions, receipts, timestamps, reconciliation, or whether reservations are outstanding.

Replays are bounded to at most 10,000 opening instrument positions and 10,000 receipt records; oversized caller-supplied books fail before inventory scanning. This is a resource limit, not evidence that the opening inventory is authentic.

Synthetic sell receipts have contiguous sequence numbers and stable receipt IDs. Exact duplicate retries are no-ops; conflicting duplicates, missing or out-of-order sequences, unknown or oversold positions, malformed money, negative fees and fees exceeding gross proceeds are rejected. Price, fees and basis use cent-valued Decimal values; this narrow research approximation does not support sub-cent prices. Cash rises by net synthetic proceeds. Weighted-average acquisition basis is allocated to shares sold, with half-even cent rounding; the final shares absorb the residual basis. Realized P&L can be negative and can exceed the planned stop loss. There is **no fill guarantee** and no effect on the existing reservation database.

SHA-256 covers the starting book and unique replayed receipts; an independently pinned expected hash can detect changes to the complete record. A self-computed digest does not authenticate a data source, protect against complete rewrite, or detect a missing tail without an independent checkpoint. No trustworthy live feed or timestamp evidence is established.

### BUY snapshot handoff (offline-only)

`OfflineSellJournal.from_buy_snapshot(path, snapshot, reconciled=True)`
accepts a typed `PaperBookSnapshot` from the existing BUY reservations
module, but **only** after all pending BUY reservations have been resolved
and cash plus fee-inclusive acquisition basis reconciles to the initial
$100,000 virtual book. It rejects absent reconciliation, halted sessions,
pending orders, nonzero reservations, available-cash mismatch, malformed
holdings, duplicate symbols and unbalanced inventory. It copies the opening
state into the synthetic SELL journal's pinned fingerprint and supports
deterministic restart replay; it does not mutate the originating BUY book.

**Critical limitation:** The two SQLite databases have no shared
transaction, lock or protected cross-book checkpoint. The BUY book could
change after the snapshot was copied, so the handoff **cannot** serve as
a trusted autonomous OMS reconciliation or an atomic order transition.
A single durable buy/sell ledger, verified market marks, automated risk
monitoring and independent QA remain required before any unattended
paper simulation.

## Durable synthetic SELL journal (development-only)

`alphora.offline_sell_journal.OfflineSellJournal` extends the pure projection
with a local SQLite WAL journal. It requires a caller-reconciled, balanced
opening $100,000 simulated book, a dated session ID, and a local file path.
It pins an opening-book fingerprint and rejects an inconsistent reopening.
`append()` validates the full projection before a single atomic receipt
insert; duplicate receipt IDs with the exact same payload are idempotent
across restarts. Gaps, conflicting duplicates, oversells, invalid money and
tampered rows fail closed without a partial insert. `snapshot()` replays the
stored receipt sequence to reconstruct cash, holdings and realized P&L.

This journal **does not** generate, route, execute, timestamp, authorize or
authenticate orders/fills. Receipt records are caller-invented. A full
database rewrite is not detectable without an external protected checkpoint.
The journal is separate from the BUY reservation database: neither has a
joint atomic transaction, real mark-to-market, trusted data provenance,
session rollover or automatic daily-loss evaluation. Therefore it is **not**
an unattended paper OMS and must not be deployed or connected to Alpaca.
The journal is for offline deterministic persistence and crash-recovery
regressions only. Independent financial review and combined BUY/SELL
integration tests are mandatory before any release assessment.

**Still missing for an actual paper OMS:** durable sell intents and cancellation, atomic buy/sell ledger integration, partial-fill and rejection lifecycle, trusted marks, daily 2% equity-loss enforcement, settled cash restrictions, pending-order reconciliation, integrated crash/restart recovery, session rollover, and operator approval. The authorized policy is unchanged: $100,000 initial simulated equity, 0.5% planned per-trade risk, 10% instrument allocation, 2% daily loss shutdown and daily rebalance. This research-only calculation does **not** enforce those trading protections.

Run the repository Python unittest discovery suite. Synthetic tests cover partial and final basis allocation, fees, losses, duplicate fills, gaps, oversells, malformed state, deterministic fingerprinting and hash mismatches. Independent financial review and combined CI checks are required prior to integration. No merge, deployment, trading or broker connectivity is authorized.
