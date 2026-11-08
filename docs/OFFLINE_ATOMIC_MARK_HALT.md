# Atomic offline portfolio observation and persistent 2% shutdown

This incremental research component is **stacked on draft PR #35**.
It consumes the existing reconciled SQLite `OfflineReservations` account
rather than introducing another independent risk database or inventing
account balances. It uses the already implemented pure bid-side valuation
in PR #35 and the already persisted `halted` key in the merged paper ledger.

## Atomic path

Call `observe_offline_portfolio_mark(book, quotes, as_of=..., reconciled=True,
session_verified=True)` explicitly while simulating a market observation.
Within the existing `BEGIN IMMEDIATE` SQLite transaction the code:

1. Reconstructs actual **synthetic** cash, held shares and pending intents
   from the book's persisted receipts. Any outstanding BUY intent is a hard
   veto; the current limited ledger has no interleaved BUY/SELL support.
2. Forms the exact one-to-one expected symbol set without trusting an
   externally supplied portfolio quantity or cash figure.
3. Runs PR #35's bid-side quote validation: ≤5 seconds old, sane
   bid/ask, ≤1% spread, no future/untrusted/extra/missing symbols.
4. Marks equity = cash + sum(held shares × bid). The current single session
   uses fixed $100,000 opening equity and fixed **2% daily** loss threshold.
5. When the inclusive threshold is breached, writes `book_meta.halted=1`
   **inside the same SQLite transaction**, re-reads the durable snapshot
   and returns only after the transaction successfully commits. A more
   favorable subsequent quote cannot remove a prior halt.

On missing or stale quotes, uncertain pending state, invalid session,
non-paper mode, or unreconciled account, the response is invalid and
provides **no usable book or mark** for trading decisions. Failed SQLite
writes roll back. Existing `reserve_buy` and synthetic-fill operations
continue to reject once the persisted halt is set.

## Critical limitations / next acceptance

- This is an **explicitly invoked offline method**, not a background
  monitor, mandatory buy/sell entry guard, price-feed consumer, or local
  trading runner. A caller that neglects to invoke it can still use the
  other existing local simulation methods. Before unattended simulation
  every decision path needs a mandatory persisted mark freshness receipt
  and strict reconciliation; this change does not claim that.
- `session_verified=True` and `SourceQuote.source_verified=True` are
  **caller assertions**, not authenticated exchange calendar or vendor
  data evidence. There is no externally authenticated timestamp, session
  halt proof, out-of-process sidecar attestation or market-data source.
  The exchange can still be closed/ halted despite those booleans.
- The current SQLite session has **no daily rollover**, all opening cash
  is $100,000, no external flows or corporate actions are modeled,
  no authenticated fills, no short/derivatives/leverage, no forced sale,
  and no automatically executable SELL intent.
- Direct use of the existing book's private `_write`/`_snapshot`/
  `_db` internals is narrowly scoped to avoid a competing ledger. It
  requires review/refactor alongside the overlapping open drafts #23/#30.
  The standalone PR #35 valuation branch and stacked ledger #30 have
  **not** been integrated; the dependency chain is not accepted for merge.
- SQLite atomicity is tested on synthetic fixtures, but power loss, vendor
  disconnection, tamper-resistant independent audit, session replay,
  crashes before invocation, and multi-process scheduler behavior need
  independent review.
- A bid mark is not a guaranteed liquidating fill price. Gaps through
  stops, illiquidity, spread blowouts, slippage and fees can exceed the
  intended 0.5% planned per-trade risk and daily threshold in practice.

No networking, brokers, Alpaca credentials, API connections, actual
market orders, deployment, merge or real-money trading. The approved
0.5% risk/trade, 10% instrument allocation, daily rebalancing and $100k
initial simulated funding remain untouched.

Acceptance requires independent financial/QA/security review of this
exact head, cross-PR integration, reliable point-in-time data/calendar,
durable observation receipts and real run-loop/restart tests.
