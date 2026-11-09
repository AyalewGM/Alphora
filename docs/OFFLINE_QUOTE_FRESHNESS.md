# Offline quote freshness and proposed review cadence

This incremental, read-only module is a **pure validation gate**, not a
running monitor or market-data client. It addresses the stale/missing price
acceptance condition in [issue #8](https://github.com/AyalewGM/Alphora/issues/8)
and the quote-quality acceptance condition in
[issue #6](https://github.com/AyalewGM/Alphora/issues/6) without duplicating
scanner or historical data-provider implementations. There are no imports of
Alpaca, HTTP, WebSocket, schedule libraries, brokerage clients, or secrets.

| Caller purpose | Suggested review cycle | Maximum quote age |
| --- | --- | --- |
| Held position (monitor stop/target *conditions*) | 5 seconds | 10 seconds |
| Watchlist | 15 seconds | 30 seconds |
| Discovery scan | 60 seconds | 120 seconds |

A 5-second *check interval* is **not** a 5-second real-time price update, a
5-second fill SLA, or a guarantee of stop execution. Actual data recency
depends on the provider and market activity. Prices for thinly traded
instruments can be stale even when the feed connection is healthy.
A price check alone cannot authorize an order or calculate an executable
fill. Distinguish exchange event timestamp from when the application received
the quote; require timestamps to be ordered and timezone aware, reject stale
and future-dated quotes, and reject crossed/nonfinite bid/ask. A label such
as SIP or IEX is **caller supplied, not vendor authenticated**.

**Regular-session only by default.** The caller must explicitly assert
`market_session="REGULAR"`; PRE, POST, CLOSED and UNKNOWN cannot pass this
initial gate. The function does not calculate exchange hours or validate a
NYSE/Nasdaq calendar, holidays, early closes or halts. A trusted provider
and an independently validated session-calendar service must own that
attestation before use with any autonomous decision. Until then, passing a
test with invented quotes proves nothing about current market prices.

API:
`assess_quote(quote, expected_symbol=..., evaluated_at=...,
market_session="REGULAR", purpose="POSITION", mode="paper")`
returns `QuoteFreshness` with `usable`, a stable reason, a suggested
check interval, and when valid, the supplied last price and measured age.
It does not retain any data or produce a signal/order/position change.
All controls are paper-only; `mode="live"` always rejects.

**Separate future approval gates:** licensed point-in-time input/vendor
provenance, independently validated US exchange calendar and authoritative
session status, spread/depth/venue coverage, quote subscriptions or
bounded polling with rate limits, persistence of source/arrival/version
and reconnect failures, monotonic timestamp/event sequencing, gap-through-
stop/partial-fill modeling, portfolio mark-to-market and persistent 2%
daily-loss halt, real order-intent lifecycle, and qualified human QA/finance
review. Unattended local simulation is not operational. No credentials,
broker connectivity, network calls or actual/real-money orders are included.

The approved owner constraints remain: $100,000 simulated initial equity,
0.5% planned risk per trade, 10% per-instrument allocation, 2% daily loss
shutdown against session-opening equity, daily portfolio rebalancing,
cash-only long-only US stocks/ETFs. This module does not modify those
settings. Proposed monitoring frequencies are **not** a new trading policy.
