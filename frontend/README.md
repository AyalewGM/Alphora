# Alphora Research Terminal (draft)

TradingView-inspired **layout**, not TradingView proprietary chart code. This is a React/Vite, read-only prototype.

## Run locally

From `frontend/`:
```sh
npm ci
npm run dev
```
Visit `http://127.0.0.1:5173`. Optionally run the FastAPI shell separately at `127.0.0.1:8000` to populate the displayed risk policy using `GET /v1/risk/policy` and `GET /v1/system/capabilities`; the development proxy is configured in `vite.config.js`.

Dependencies are pinned in `package-lock.json` (npm lockfile v3) to make the complete installed tree reproducible. Use `npm ci` locally and in CI; changes to runtime dependencies require a reviewed lockfile diff. The lockfile constrains resolution but does not independently establish package trust or vulnerability freedom.

Run `npm test` to execute dependency-free Node contract tests. The terminal marks the local backend verified only when **both** read-only endpoints return paper-only capabilities and the approved risk settings; otherwise it remains visibly offline. Even a verified local API does not mean historical or live market data are available.

## Data provenance and safety

**All watchlist prices, changes, and candles are deterministic illustrative values generated in the browser, NOT market prices, a backtest result, or simulated account performance.** They must not inform real trading or paper-order eligibility. No market feed or brokerage service is contacted. UI offers no buy/sell controls, credentials, execution paths, portfolio mutations, or order APIs. The only requested API resources are the two local reference GETs: risk policy and system capabilities. Neither supplies authenticated account state. If unavailable, the screen labels itself offline and displays the approved policy as a local preview.

Before enabling operational portfolio displays: implement separately reviewed, authenticated, timestamp-validated read-only API contracts for point-in-time bars, symbols, signed equity snapshots, watchlists, decisions, and journals. Replace demo bars completely; add source/as-of/warmup/staleness indicators, authorization and accessibility tests. The frontend must never compute or override authoritative risk controls.


## Draft-only configurable paper limits (issue #65)

The **Draft paper-risk settings** form now previews the full six-key `paper-risk-proposal-v1` structure from backend PR #66 (already merged into main). Fields are **new-portfolio starting virtual equity**, planned risk per trade, instrument allocation ceiling, daily-loss halt threshold, and rebalancing cadence. Existing approved policy defaults remain USD 100,000 / 0.5% / 10% / 2% / daily.

- Every proposed risk limit can only **tighten** the owner's current hard ceilings: trade risk at most 0.5%, per-instrument allocation at most 10%, daily loss at most 2%. Percentages and USD cents use BigInt/decimal-string validation; malformed, oversized, missing, unknown, non-finite and float representations fail closed. No JavaScript floating-point comparisons are used for policy authority.
- The UI **never** sends, saves, applies, executes or persists these proposed values. Nothing in the UI can change an existing paper portfolio balance, activate a policy or release a halt. Weekly/manual scheduling is not yet wired.
- **Portfolio monitor:** cash, equity, positions, P&L, emergency halt, reconciliation, effective policy revision, and decision journal explicitly display **UNAVAILABLE / UNKNOWN / NOT VERIFIED** until a separate independently reviewed backend contract is available. Initial virtual equity in the policy header is not the cash or equity balance.
- The [proposed contract](docs/paper-monitor-contract.md) documents required authoritative read-only account/journal state and backend responsibilities. It is not an available endpoint or execution feature.
- Validation failures identify the invalid field, focus it for keyboard correction, and announce actionable guidance. Percent entries accept up to four decimal places (a conservative UI subset of the backend wire domain).
- Keyboard access: a visible-on-focus **Skip to main content** link avoids decorative shell navigation; selected research tabs and demo watchlist rows expose `aria-pressed`, with visible focus styles. At narrow mobile widths, the verification status wraps instead of overflowing the top bar, and horizontal research-tab scrolling retains keyboard focus and 44px button targets (layout still needs manual 320px/browser/screen-reader verification). Decorative rail glyphs are hidden from assistive technology. Static checks in `terminal-a11y-contract.test.mjs` guard this markup but **do not replace manual keyboard, mobile or screen-reader verification**.
- A successful `GET /v1/risk/policy` + `GET /v1/system/capabilities` check is labeled **reference GET verified, portfolio unverified**. It is never evidence of effective account policy, balances, journal or emergency-halt status.
- Run `npm test` and `npm run build` locally. Verify keyboard navigation, narrow viewports, and no activation or broker/network paths. Independent review remains required.

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

## Browser security boundary

The local terminal declares a restrictive Content Security Policy in `index.html`: same-origin scripts and requests only, no plugin objects or form submissions, and limited local styles and images. The policy is defense-in-depth, not a substitute for sanitizing imported market data. It permits inline styles for chart rendering; this is an explicitly accepted limitation. The static meta policy cannot enforce `frame-ancestors`, and local Vite hot-reload connectivity must be verified separately if WebSocket policies change.

## Acceptance checks

- Run `npm test` and `npm run build` in CI.
- Confirm unknown/missing backend leaves the preview visibly offline.
- Confirm every synthetic chart/watchlist surface is visibly marked DEMO / illustrative, and local historical bars say user-supplied / unauthenticated.
- Run strict local daily-bar negative tests and ensure any rejected import falls back without retained local bars.
- Confirm there are no order buttons, broker URLs, credential fields or live-data claims.
- Independent review of accessibility, responsive behavior, supply-chain dependencies and data provenance required prior to merge.

No broker connectivity, deployments, trades, or automatic merges authorized.
