# Alphora Research Terminal (draft)

TradingView-inspired **layout**, not TradingView proprietary chart code. This is a React/Vite, read-only prototype.

## Run locally

From `frontend/`:
```sh
npm install
npm run dev
```
Visit `http://127.0.0.1:5173`. Optionally run the FastAPI shell separately at `127.0.0.1:8000` to populate the displayed risk policy using `GET /v1/risk/policy` and `GET /v1/system/capabilities`; the development proxy is configured in `vite.config.js`.

Run `npm test` to execute dependency-free Node contract tests. The terminal marks the local backend verified only when **both** read-only endpoints return paper-only capabilities and the approved risk settings; otherwise it remains visibly offline. Even a verified local API does not mean historical or live market data are available.

## Data provenance and safety

**All watchlist prices, changes, and candles are deterministic illustrative values generated in the browser, NOT market prices, a backtest result, or simulated account performance.** They must not inform real trading or paper-order eligibility. No market feed or brokerage service is contacted. UI offers no buy/sell controls, credentials, execution paths, portfolio mutations, or order APIs. The only requested API resource is the local read-only risk policy. If unavailable, the screen labels itself offline and displays the approved policy as a local preview.

Before enabling operational portfolio displays: implement separately reviewed, authenticated, timestamp-validated read-only API contracts for point-in-time bars, symbols, signed equity snapshots, watchlists, decisions, and journals. Replace demo bars completely; add source/as-of/warmup/staleness indicators, authorization and accessibility tests. The frontend must never compute or override authoritative risk controls.

## Acceptance checks

- Run `npm test` and `npm run build` in CI.
- Confirm unknown/missing backend leaves the preview visibly offline.
- Confirm every synthetic chart/watchlist surface is visibly marked DEMO / illustrative.
- Confirm there are no order buttons, broker URLs, credential fields or live-data claims.
- Independent review of accessibility, responsive behavior, supply-chain dependencies and data provenance required prior to merge.

No broker connectivity, deployments, trades, or automatic merges authorized.
