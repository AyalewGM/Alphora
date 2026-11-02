# Offline benchmark comparability

The `compare_with_spy()` helper compares a tested strategy against:
1. A **same-exposure** SPY buy-and-hold proxy using the same initial cash, maximum allocation, simulated transaction costs and aligned dates.
2. A **fully invested** SPY buy-and-hold price-only proxy (100% allocation).

The first comparison isolates part of the allocation difference; the second shows the familiar passive market exposure. Both are single-instrument, whole-share, next-open simulated fills.

**Neither proxy is an authoritative SPY total-return benchmark.** Source data must be price-adjusted appropriately, dividends and corporate actions reconciled, and market calendars validated before reporting investment-performance claims. A fully invested proxy intentionally does not follow the live/paper 10%-per-instrument policy; it is offline research only. No Alpaca connection or broker execution exists.

The helper rejects mismatched date indexes rather than silently aligning or forward-filling. No claim of statistically significant alpha is supported. Portfolio-wide risk limits and stop-loss logic are not implemented here.
