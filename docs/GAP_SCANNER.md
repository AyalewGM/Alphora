# Alphora gap-up / liquidity scanner: offline first slice

Tracking: [Issue #6](https://github.com/AyalewGM/Alphora/issues/6).

This pure function **does not discover ticker symbols, connect to a market-data
vendor, query news, access Alpaca, authorize trading, or classify fraud**.
It consumes independently assembled candidate snapshots and returns a ranked,
auditable research list. The list is NOT a security clearance, a recommendation,
or paper-order eligibility. Its sample data in tests are entirely synthetic.

## Calculations and gates

- For PREMARKET, gap % is (latest available premarket price / previous
  *regular-session* close - 1) × 100, labeled **indicative**. The quote/reference
  must be from the same Eastern-local session date before 09:30 ET.
- For OPEN, gap % uses a vendor-verified opening price in 09:30–09:30:59 ET;
  this is a completed regular-session opening gap. The snapshot can be later.
- ADV20 and average daily dollar volume must be derived from *completed*
  prior regular sessions only. This module trusts the caller's independently
  validated history window; it rejects a same-day history cutoff.
- Relative volume is cumulative volume (premarket volume or regular-session
  volume as appropriate) / a *same-session, same-clock-time historical
  baseline*. An absent baseline yields CAUTION, never a fabricated RVOL.
- The default *research* gates are +3% gap, at least $5 price,
  1 million ADV20 shares, $20 million average daily dollar volume,
  100,000 premarket shares, >=2× matched RVOL, and <=1% bid-ask spread.
  They are provisional filters, not evidence of predictive value.
- Only NYSE/Nasdaq common shares are eligible for WATCHLIST. IEX-only
  coverage produces CAUTION; unknown source/unsupported listings excluded.
  SIP/full-market entitlements and data licensing must be separately verified.
- The caller must attest to session-calendar validation and consistent
  corporate-action treatment. Both false statuses exclude a candidate.
- Fresh quote <=120 seconds. News/catalyst must have a verified publication
  timestamp <= scan time. No timely catalyst is CAUTION, not proof of fraud.
- Float <10 million, extreme gap >=25%, sharp reversal, dilution signals
  or unknown promotional/float evidence require caution. Positive promotional
  activity flag excludes from the top watchlist; it is **not** a factual legal
  determination that manipulation occurred.

## Implementation boundaries and pending work

1. Add point-in-time vendor adapters with record provenance, SIP entitlements,
   symbol eligibility and session-calendar/corporate-action validation.
2. Compute true time-aligned intraday baselines and prove that neither current
   day's data nor subsequently published news leaks into historical scans.
3. Collect sourced event/news/filing, promotions and free-float evidence.
   Unknown is not equivalent to false.
4. Build read-only, authenticated and locally scoped query API and UI
   after authorization boundaries are implemented. Do not expose an anonymous
   scanner endpoint or any order-placement endpoint.
5. Evaluate sample bias, false positives, missed pump-and-dump cases, stale
   data, halts, symbol changes, delayed feeds, holidays/half days, splits,
   and adjusted/unadjusted previous-close consistency on independent fixtures.
6. Integrate deterministic portfolio risk checks and persistent paper-order
   safeguards in a **separate future milestone**, only after owner approval.

No real market activity, P&L, prediction, or backtest results are claimed.
