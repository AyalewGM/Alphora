# Offline point-in-time daily-bar receipt

This is a research-only *candidate* input validation gate. No API, broker or data downloader exists in this module.

## Usage

Supply two or more chronologically ordered `ObservedDailyBar` items, each containing an OHLCV `Bar`, the vendor-attested `first_seen_at`, and the `version_available_at` for **that exact revision**. Call `inspect_daily_snapshot(symbol, observations, asset_type="ETF", source="vendor_id", source_revision="batch_id", price_basis="RAW_UNADJUSTED", as_of=decision_time)`. Times must be timezone-aware. `as_of` is the research decision cut, not the computer clock.

The validator rejects bad OHLCV, unsorted or duplicate sessions, malformed metadata, timestamps before 16:00 Eastern on the record day, versions published before initial observation, versions later than `as_of`, and revisions not strictly available before the *next recorded bar's* 09:30 Eastern open. A canonical SHA-256 binds OHLCV, vendor identity and revision, price basis, and availability metadata. Pass `expected_sha256` to detect changed inputs. This hash is not a signature or proof of vendor honesty.

## Important limitations

- Feed timestamps and versions are **caller asserted**. Validate against independently stored vendor receipts, vendor revisions, historical universe membership and licensed feed provenance before describing real data as point-in-time.
- There is no session/holiday, special-hours or halt calendar; 16:00/09:30 Eastern gates are deliberately conservative, but do not prove an exchange session occurred. Do not infer a completed bar from date and wall-clock alone.
- Price basis is metadata, *not* adjustment computation. Split, dividend, distribution, ticker-change, delisting and survivorship accounting remain absent. Current backtesting is single-asset and does **not** enforce the 0.5% planned risk limit, 2% portfolio daily halt, or execution slippage.
- This receipt does not approve historical strategy performance, establish optimal trades or authorize any orders. Live and paper broker connectivity, brokerage credentials, deployment and automated execution remain prohibited.
- Run offline tests with `python -m unittest discover -s tests -v`. Independent data vendor and replay testing is still required before integrating this receipt into a professional backtester.

Next gate: attach authenticated vendor arrival receipts and revisions; verify full historical session calendars, corporate-action histories and delisted-universe completeness. Only then add cross-asset/as-of replay integration and benchmark testing.
