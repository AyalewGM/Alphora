# Alphora PR #2: offline historical data and backtesting foundation

This is a **research prototype**, not a production backtester or order execution engine.

## Data
Use CSV with columns `date,open,high,low,close,volume`; strict ascending unique dates, finite positive prices, valid OHLC and nonnegative volume. No online market-data provider has been connected. Inputs require external provenance checks, vendor licensing, adjusted/unadjusted consistency, exchange calendars, splits, dividends, delisted securities and survivorship-bias mitigation before reliable market conclusions.

## Strategies
`trend`: closing price above trailing SMA; `momentum`: closing price above close 20 sessions ago; `mean_reversion`: close below prior-20-session mean; `buy_hold`: always own when affordable.

## Model
Signals use **observed closes up to t-1**, orders fill at next open t. Cash-only, long-only, whole shares, maximum 10% instrument notional at entry, fixed per-side transaction cost (default 5 basis points). Equity measured at closing prices. Returns, maximum drawdown, zero-risk-free-rate annualized Sharpe, transactions and full equity curve are calculated.

## Deliberate limitations
Single instrument; no data downloader, splits/dividends, delisting payouts, session/calendar validation, slippage or spread model beyond simplified bps, stop handling, multi-asset allocation, portfolio daily halt, partial fills, walk-forward runner or real brokerage. In particular the PR #1 0.5% planned trade stop risk and 2% daily shutdown are **not** enforced by this research-only simulator: integrating these controls is required before paper orders. The 10%-exposure buy-hold reference is not equivalent to fully invested SPY.

No claimed alpha, trained ML model, real brokerage access or live trading capability.
