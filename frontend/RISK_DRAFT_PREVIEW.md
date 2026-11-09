# Draft-only configurable paper-risk settings

The research dashboard offers two editable **preview-only** inputs:
- Planned risk per trade, default **0.5%**
- Maximum allocation per instrument, default **10%**

`frontend/src/risk-draft.js` validates canonical decimal strings using
integer arithmetic (not floating-point conversion). It accepts more than
0% up to 2% planned trade risk, and more than 0% up to 100% instrument
allocation, at up to four decimal places of percentage precision.
The upper limits are input-validation ceilings, **not recommended risk
settings**. The fixed 2% daily-loss halt and $100,000 initial simulated
equity do not change.

The **Validate preview** action produces a draft value only in React
component memory. Editing either input invalidates the old preview.
**Reset defaults** restores the existing 0.5% and 10% draft values.
No storage is used (no cookies, localStorage, files, or server writes).
There are no POST/PUT/PATCH requests and no broker/Alpaca connections.
The read-only backend contract still only accepts the current approved
policy and still reports execution disabled. The top metrics show that
backend-approved policy, not a proposed draft. This UI does not activate
a trading session, mutate a portfolio, or authorize automated orders.

**Still required before unattended paper simulation:** independent review
of backend PR #23, a trusted and audited operator settings persistence
contract, consistency between persisted policy and execution risk checks,
session-transition rules, full BUY/SELL ledger reconciliation, negative
tests and an explicit separate paper-broker-connection authorization.
Do not merge this preview UI on the theory it enables live or paper
execution.
