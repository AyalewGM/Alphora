# Paper-only risk settings preview — frontend contract (#65)

**Status: local preview only; no settings are saved or applied.** This document describes the frontend delivered in merged PR #67 and the inert `paper-risk-proposal-v1` wire contract from merged PR #66. It is not an effective risk-policy API, trading setting or authorization to execute orders.

## Five editable proposal controls

| User-visible control | Approved default | Preview validation |
| --- | --- | --- |
| Starting simulated equity (USD) | $100,000 | Positive USD amount, at most two decimals; **new portfolios only**, never an existing ledger reset |
| Planned risk per trade | 0.5% | Greater than 0 and **at most 0.5%** |
| Maximum per-instrument allocation | 10% | Greater than 0 and **at most 10%**, including pending exposure in any future backend enforcement |
| Daily-loss shutdown threshold | 2% | Greater than 0 and **at most 2%** of verified session-opening equity; a preview does not clear a halt |
| Rebalancing cadence | Daily | `daily`, `weekly` or `manual` **proposal only**; no scheduler is active |

These 5 controls yield a validated **6-key** object: `schema: "paper-risk-proposal-v1"`, `starting_equity_usd`, `risk_per_trade_fraction`, `max_instrument_fraction`, `daily_loss_fraction`, and `rebalance_frequency`. All money and fractions are canonical decimal **strings**, not JSON numbers. Input parsing uses BigInt and exact decimal arithmetic to reject float rounding, scientific notation, extra or missing keys, non-finite values and values above the approved hard ceilings. The UI accepts at most four fractional digits in percentage input, a conservative subset of the backend proposal contract.

**Validate preview** compares the unsaved proposal to **approved reference defaults and hard ceilings**, not an authenticated effective account policy. Changing an input removes the previous validation preview; **Reset local draft inputs only** replaces form values with the defaults and does not touch a portfolio. The preview lives in React memory: it is not persisted, transmitted, approved, or scheduled. No form action can loosen a safety ceiling, submit orders, override reconciliation, activate trading or reset existing simulated equity.

## Dashboard data, availability and safety boundaries

The terminal retains the owner-approved reference defaults: $100,000 virtual starting equity, 0.5% planned trade risk, 10% instrument allocation, 2% daily-loss halt and daily rebalancing. Reference data are **not portfolio balances or effective policy revisions**. Existing same-origin GET-only endpoints `/v1/risk/policy` and `/v1/system/capabilities` expose static reference policy/capabilities, **not** account, journal, effective-policy, order or halt data.

The paper portfolio monitor deliberately shows **UNAVAILABLE** for cash, equity, positions and profit/loss; **UNKNOWN** for the emergency halt; and **NOT VERIFIED / NOT AVAILABLE** for reconciliation, journal and effective policy revision. An unavailable journal does **not** prove no orders or fills occurred. The proposed authenticated read-only account/journal interface in [docs/paper-monitor-contract.md](docs/paper-monitor-contract.md) is **design only**; no account endpoints are implemented or called.

The app's watchlist and built-in candlesticks are **synthetic illustrative data**, not market prices. A locally imported daily snapshot is user-supplied and unauthenticated. No brokerage adapter, credentials, Alpaca connection, market-data feed, order placement, settings write, deployment or real-money trading is enabled.

**P0 #42 remains open:** unattended multi-session paper execution cannot be called ready until durable session-opening equity, daily loss halts, rollover and reconciliation have independent acceptance. A validated draft cannot release that gate.

## Verification and review

Run `npm test` and `npm run build` from `frontend/` when a Node environment/CI runner is available. Also check 320px mobile widths, keyboard focus and visible error guidance, horizontal tables, screen-reader announcements, unknown account state, and a failed/offline reference GET. Static source tests do not substitute for actual browser accessibility testing.

Independent Frontend/Accessibility, Financial and Security QA must review the exact head and eventual integration build. No self-approval, auto-merge or deployment.