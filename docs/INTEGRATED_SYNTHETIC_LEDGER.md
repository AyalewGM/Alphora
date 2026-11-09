# Single-SQLite synthetic BUY/SELL accounting — development slice

This draft extends the protected offline BUY reservation ledger from PR #23.
It adds a `hypothetical_sells` table to the **same SQLite database** and an
atomic `record_hypothetical_sell(fill_id, symbol, qty, price, fee=...)` method.
Receipt IDs are idempotent across process restarts; conflicting ID reuse fails.
Each hypothetical SELL, recorded cash increase, retired weighted-average
purchase basis, and realized P&L is reconstructed within the existing
`BEGIN IMMEDIATE`/WAL transaction boundary. The snapshot reconstruction
rejects oversells, receipt-sequence gaps, invalid or modified stored receipts,
and cash conservation violations.

For this first incremental integration slice, the book enforces a **one-way
BUY phase followed by a SELL phase**. All BUY reservations must be completed
or cancelled before any SELL receipt. After the first SELL, no new BUY
reservation or BUY fill is accepted. This is deliberate fail-closed
protection against reordering hypothetical receipts without a complete
interleaved event ledger. This limitation means the module is **not yet
an unattended daily rebalancing engine**. A future audited unified
time-ordered event journal must support arbitrary validated buy/sell cycles,
simultaneous orders, corporate actions and daily session rollover.

**This method is only a synthetic receipt bookkeeping API.** It creates no
sell intent, authorizes no order, authenticates no fill, and does not check
external price/timestamp provenance, current exchange session, position
mark-to-market or stop trigger. It cannot validate whether hypothetical
SELLs should happen. It therefore cannot safely run an unattended trading
loop yet. It deliberately rejects new SELL receipts after the manual/observed
halt, though actual risk-controlled position liquidation policies will
need a separately reviewed design.

The original $100,000 simulated initial book, default 0.5% planned
per-trade risk, default 10% instrument cap (both configurable on the
underlying PR #23), fixed 2% daily-loss shutdown, and daily rebalancing
requirement remain unchanged. P&L from these receipt records is **realized
accounting only**; current equity still requires independently trusted
price marks. Synthetic fills are not brokerage executions.

This is a **stacked draft** based on PR #23. The independent read-only SELL
projection in PR #28 is a separate reference implementation; it is neither
merged nor imported as a broker interface. Integration and any migration
from that reference must receive independent financial and security/QA
approval. No broker API, Alpaca credentials, remote endpoints, order
submission, merge, deployment or real-money support are introduced.

Acceptance for the next slice: unified chronological event ledger with
repeated BUY/SELL cycles, account marks and 2% halt propagation, session
rollover, recovery from crashes, pending SELL reservations, explicit
simulated order state-machine and scheduler. Only then evaluate unattended
local paper-simulator readiness.
