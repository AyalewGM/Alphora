# Paper venue: deterministic per-share read views

## Synthetic-only defect and correction

`PaperBrokerAdapter.advance()` now uses fixed-context settlement arithmetic
(draft #48), but `PaperBrokerAdapter.positions()` and `_Order.view()`
previously divided cost/fill value using the **ambient caller Decimal precision**
before cent-quantizing. A low-precision reader could therefore receive
different position cost and reported average fill from an identical ledger.

Synthetic fixture: a 33-share BUY at a $49.70 mark with 37bps simulated
slippage and a $1 fee fills at $49.88, leaving cash $98,352.96.
Total position basis is $1,647.04, so the cent-rounded per-share basis is
**$49.91**; average filled execution price is **$49.88**. A caller precision
of 3 previously returned **$49.90** for both read fields.

The read-only `_cent_average()` helper now divides using a local
50-digit, half-even Decimal context, with deliberate *per-share cent rounding*.
This avoids ambient rounding/trap differences; it does not round cash or
the authoritative aggregate cost basis. Regression compares precision
3 / 6 / 28 / 80 and asserts stable order/position views and cash.

## Scope and acceptance boundary

This draft PR is stacked on #48 → #47 → #46 and does **not** change venue fills,
book ledger, risk settings or broker connectivity. It is synthetic-only and
requires exact-head independent Devin/Muse review plus successful GitHub
Actions and dependency checks before any human-directed merge. It does not
resolve extreme-Decimal edge cases beyond fixed-context capabilities,
multi-order atomicity, restart persistence, 2% per-session loss rollover (#42),
licensed point-in-time data or actual LEAN runs (#20).

Alpaca remains disconnected. No broker credentials, API connectivity,
deployment, trading, automatic merging or live execution is authorized.
The simulated policy stays $100,000 initial equity; <=0.5% planned trade risk,
<=10% instrument notional, a 2% session-opening-equity daily halt and
daily rebalancing.
