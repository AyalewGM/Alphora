# Offline paper decision proposals — Issue #8

The alphora.decision module emits deterministic **paper-only proposals**. No
broker, market-data vendor or network access; no credentials, orders, capital
reservations, deployment or real-money pathways are implemented.

## Entry contract

A valid entry requires:
- reconciled cash-only long-only PaperAccount: equity movement since session
  open must match day_pnl within $0.01 and cash may not exceed equity;
- both reported and equity-derived daily losses below the 2% shutdown limit;
- WATCHLIST scanner status and VERIFIED catalyst from independent source checks;
- symbol-bound ask, confirmed regular session and timestamped quote age <=120s;
- positive ask, protective stop below ask, adequate unreserved cash and
  verified same-instrument exposure including pending purchases.

The candidate's indicative reference price is not used for sizing. Position
quantity is the floor of the minimum of 0.5% current equity divided by
ask-to-stop risk, remaining 10% instrument allocation divided by ask, and
unreserved cash divided by ask. The risk guard independently rechecks the
proposal. Price improvement or worsening at actual fill is not guaranteed.
Cash reservations and existing-plus-pending instrument exposure are added or
subtracted with exact Decimal accounting: if context precision would round away
even a small reserved amount, entry sizing vetoes the proposal rather than
silently inflating buying power or understating instrument exposure. The caller
must supply reconciled and representable balances; this is not an atomic
reservation or a substitute for future OMS accounting.

An entry intent key derives from session, ticker, timestamp, ask, stop and
quantity. Repeated identical inputs give the same key, but new quotes can
produce new keys. **Only a future persistent OMS may atomically reserve cash,
check uniqueness, reconcile fills and cancel stale intents.** These
read-only proposals must never be directly sent to a broker.

## Exit contract

A valid sell proposal uses a symbol-bound, fresh, regular-session bid, actual
reconciled average fill basis, positive caller-provided position reconciliation
version and whole filled shares. A configured gross take-profit percentage is
measured against average fill price; a protective stop is checked first.
Break-even and raised stops remain valid. Pending exited shares are excluded.

A protective stop can still propose a sell for the remaining unencumbered
shares when another exit is pending. When there is nothing unencumbered, or a
take-profit event overlaps pending exits, the system returns
BLOCKED_REQUIRES_RECONCILIATION, which must be surfaced as an OMS alert rather
than silently treated as a regular HOLD.

Exit intent keys include position ID, symbol, reconciliation version, reason
and unencumbered quantity. Thus a partially filled/cancelled target cannot
accidentally suppress a later stop with the same idempotency key.

## Limitations and mandatory future gates

The entry/exit intent layer rejects untyped snapshots and handles Decimal
arithmetic overflow fail-closed; an extreme or corrupted paper snapshot must
never produce an affirmative entry or sell intent because an arithmetic
exception escaped validation. These are regression-only synthetic tests,
not verification of market data or broker execution.


The trigger is not a guaranteed fill, stop, or net profit. Spread, slippage,
market gaps, commissions, corporate actions, halts, half-days, concurrency,
partial fills and broker-side state need independently validated accounting
and resilient paper-only OMS behavior before any connectivity. Daily strategy
rebalance does not prevent more frequent protective monitoring.

Unit tests cover independent reviewer findings H1/H2 and M1-M4, including
partial-fill-then-stop, inconsistent cash/equity/P&L, quote identity,
pending exits, ask-based sizing and break-even stops. A strict quote-freshness
ceiling is enforced. **Independent exact-head review is still mandatory.**

Paper-order submission and even paper brokerage connectivity remain disabled
until explicit separate owner authorization. Real-money trading remains
strictly prohibited.
