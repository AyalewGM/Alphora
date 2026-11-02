# Offline paper decision proposals — Issue #8

`alphora.decision` adds **pure, deterministic proposal helpers** only. It does not connect to Alpaca, fetch market data, reserve funds, submit an order, cancel an order or mutate the account. The absence of an execution adapter is an intentional safety boundary.

## Entry sizing

`propose_entry()` requires a reconciled paper account, scanner `WATCHLIST` classification, `VERIFIED` catalyst, fresh point-in-time observation and a valid protective stop below the proposed entry. All monetary inputs use `Decimal`.

For long-only whole shares it uses the floor of:

- 0.5% of *current* equity / planned per-share loss,
- (10% of current equity − existing instrument notional − same-instrument pending buy notional) / proposed entry price,
- (available simulated cash − cash reserved for pending buys) / proposed entry price.

It then calls the existing paper-only `validate_buy()` guard. The current `validate_buy` policy prohibits new buys at or beyond the daily 2% loss threshold. The $100,000 initial virtual equity and daily rebalance policy remain unchanged. These proposals are not orders; **the future durable OMS must atomically reserve/revalidate** cash/exposure and persist an idempotent intent just before any authorized paper submission. A concurrent proposal can otherwise reuse the same cash. Real fill prices and commissions may differ from proposed prices. This is not a portfolio optimizer or broker reconciliation engine.

## Exit proposals

`propose_exit()` compares the **bid** against a configurable percentage above the *reconciled average fill price*, and uses a separately recorded fixed protective stop. An adverse stop trigger takes priority. The function requires a verified regular session, a timestamped non-stale bid/ask, whole-share filled position and no outstanding exit quantity; it returns a stable sell-intent key, never places or guarantees an order. Gaps can produce losses larger than the stop distance; spreads, liquidity and slippage can prevent a profitable fill. Take-profit is **gross**, not net after transaction costs or taxes. Split adjustments, tax lots, price limits, halts and partial-exit lifecycle must be handled in the future OMS.

Daily rebalance applies to strategy allocation decisions, **not** to protective monitoring. The example percentage is configurable in research and should not be interpreted as a backtested optimal profit target.

## Validation / remaining gates

Tests cover risk, instrument and cash reservations, halt behavior, unknown catalysts, freshness, stop precedence, take-profit threshold, pending exits, idempotency keys and corrupt positions. Still needed: point-in-time full-market data, catalyst provenance, validated broker/account snapshots, atomic reservation, persistent paper-only OMS, restart reconciliation, approval workflow, commission/slippage-aware simulation, splits/dividends, market-calendar enforcement, independent financial review and shadow replay.

No live trading endpoint or credential is implemented or authorized. Do not deploy, place paper orders, or connect any broker without separate authorization.
