# Offline market-session monitoring planner — premarket + regular

This pure module is a **deterministic scheduling and risk-evaluation gate**
for future unattended local paper simulation. It **does not run a timer**,
fetch quotes, query news, issue orders, synthesize fills or touch accounts.

## Approved windows (America/New_York exchange clock)

- **04:00–09:30 ET: PREMARKET** — monitor existing holdings, discovery,
  watchlists and news; **no simulated order execution**.
- **09:30–16:00 ET: REGULAR** — evaluate source-verified quotes and
  potential SELL intents under separate downstream financial/risk checks;
  published **13:00 early close** respected.
- **After close, before 04:00, market holidays and exchange halts:** no
  new monitoring work scheduled. The caller must supply an independently
  verified per-day exchange calendar, including early closes and halts.
  The code **does not infer an open exchange from the weekday** and does
  not authenticate a caller's calendar claim. UTC offset/DST conversion
  follows the installed America/New_York zone database.

## Proposed polling targets (not running yet)

| Activity | Target |
| --- | --- |
| Open positions / risk checkpoints | Every 5 seconds |
| Watchlist | Every 15 seconds |
| Discovery scanner | Every 60 seconds |
| News | Every 300 seconds |

These are intended **due-work flags**, not actual network calls.
A real service must reconcile provider quotas, data freshness,
reconnect/outage behavior and per-fill urgent risk evaluation. Checking
risk every five seconds **does not guarantee fill at a stop price**.

## Fail-closed boundaries

Any missing/invalid calendar/session, cross-day or future checkpoint,
naive clock, exchange halt, unknown quote source, unverified symbol,
nonpositive quote, wide bid/ask spread, or held quote older than five
seconds disables regular-session exit evaluation. The supported
regular-session bid/ask spread ceiling is currently **1%** (a technical
gate, not an assurance of liquidity). No premarket executable
eligibility exists in this milestone.

The `regular_exit_evaluation_allowed` flag is only a *permission to
evaluate* downstream strategy logic. It is **not** permission to record
an invented SELL receipt without further authenticated market/portfolio
checks or to send an external order. The 2% daily-loss halt, $100k
initial equity, configurable default 0.5% per-trade risk and 10%
instrument allocation, and daily rebalance requirement remain
independent constraints.

## Dependency / release blockers

This draft PR stacks on #30 (itself stacked on #23). It introduces no
new database, scheduler daemon, API endpoint, third-party provider,
credentials, Alpaca adapter, trade, deployment or merge. Independent
calendar/data, financial, security and QA reviews remain required.
Before unattended local simulation: durable tick checkpointing,
authenticated/licensed provider, source-provenance receipts,
per-update portfolio mark-to-market and continuous 2% loss halt,
idempotent end-to-end decisions, an interleaved BUY/SELL event state
machine, session rollover, crash/reconnect replay, and complete
negative no-broker tests.
