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


## Local historical daily-chart preview

Use **Import JSON** to select a small user-supplied, offline daily-bar snapshot. This is a client-side preview only; the file is not uploaded. The historical view is identified as **USER-SUPPLIED** and **NOT AUTHENTICATED**, unlike the synthetic demo watchlist. A declared SHA-256 receipt is shown only as metadata: the frontend does **not** independently recompute, verify or authenticate it. Never regard this as trading-grade or independently vendor-verified history.

Required JSON structure (all amounts decimal strings, times timezone-aware, dates daily):

```json
{
  "symbol": "SPY",
  "asset_type": "ETF",
  "source": "local_review_required",
  "source_revision": "v1",
  "price_basis": "RAW_UNADJUSTED",
  "as_of": "2026-11-02T12:00:00Z",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "bars": [
    {"day":"2026-10-29","open":"100","high":"105","low":"98","close":"103","volume":500,"first_seen_at":"2026-10-29T20:01:00Z","version_available_at":"2026-10-29T20:10:00Z"},
    {"day":"2026-10-30","open":"104","high":"106","low":"101","close":"105","volume":600,"first_seen_at":"2026-10-30T20:01:00Z","version_available_at":"2026-10-30T20:10:00Z"}
  ]
}
```

The example is **illustrative only**; its hash is a dummy placeholder and does not authenticate the bars. Client checks include bounded record counts/file size, strictly increasing days, exact decimal syntax, OHLC consistency, valid volumes, timezone-aware observation timestamps and conservative next-open lookahead screening (09:30 America/New_York). The daily chart draws volumes in a **separate fixed-height strip**, normalized relative to the largest displayed bar: this prevents realistic million-share totals from overflowing the SVG or obscuring prices. Volume heights are relative graphical proportions, **not** absolute volume counts, opening-auction liquidity, guaranteed fills or a paper-trading eligibility check. The exchange calendar, early closes, vendor attestations, file signatures, receipt integrity, corporate actions and historical universe membership remain **unverified**.

For production-quality historical charts, use a separately reviewed API that exposes only authenticated, independently checked `DataSnapshot` exports. Do not convert this manual import into signal, sizing, or order eligibility.

## Acceptance checks

- Run `npm test` and `npm run build` in CI.
- Confirm unknown/missing backend leaves the preview visibly offline.
- Confirm every synthetic chart/watchlist surface is visibly marked DEMO / illustrative, and local historical bars say user-supplied / unauthenticated.
- Run strict local daily-bar negative tests and ensure any rejected import falls back without retained local bars.
- Confirm there are no order buttons, broker URLs, credential fields or live-data claims.
- Independent review of accessibility, responsive behavior, supply-chain dependencies and data provenance required prior to merge.

No broker connectivity, deployments, trades, or automatic merges authorized.
