# Bounded offline point-in-time research manifest (issue #20)

This **synthetic-only, no-network** module provides a vendor-neutral typed manifest for data owner/operator attestation, distinct from the OHLCV bar validation in merged PR #2 and the draft snapshot replay in PR #17. Call `inspect_market_manifest` with a `MarketManifest`, separately pinned `expected_artifact_sha256` and `expected_calendar_sha256`; optionally pin an `expected_manifest_sha256` on a repeat. The result is a deterministic local fingerprint, **not** an authenticated vendor signature or a real-market accuracy certificate.

The manifest records stable asset identity independent of ticker, stock/ETF classification, venue, listing/delisting dates, historical ticker periods, as-of universe inclusion/exclusion and reason code, vendor/feed/revision/grant declarations, delivered/revision availability timestamps, source and version of an explicitly supplied exchange calendar, and exact bar-to-session alignment. Sessions distinguish declared regular days from declared early closes and must be explicit. The example fixtures omit a US exchange holiday and declare an early close; this is *synthetic evidence only*, not sourced exchange-calendar truth. Unsupported partial halts and irregular opens fail closed rather than being guessed.

The validator rejects symbol-period start/end dates and delisting effective dates later than the US Eastern date of `as_of` (`FUTURE_INSTRUMENT_EVENT`). Without independent announcement/availability receipts, allowing those future events could leak later-known ticker or survivorship information into an earlier snapshot. This conservative bound does **not** authenticate when an already-effective change was first reported, nor does it prove that same-day events were available at the exact snapshot time. An instrument declared active (`delisted_on=None`) must also retain an **open-ended final ticker period**. A closed last period is rejected as `INVALID_SYMBOL_HISTORY` even if its end date still covers every declared bar; a genuinely delisted instrument needs a matching explicit delisting date. This prevents quietly treating an incomplete active security master as a complete symbol history.

The validator additionally rejects `PREMATURE_ARTIFACT_DELIVERY`: a manifest claiming complete daily bars through its final declared session must not attest that the corresponding artifact was delivered before that session closed. This is only a chronological consistency check on caller-supplied times, **not** proof that the vendor actually delivered a complete file or that a historical bar was usable at any earlier strategy-decision timestamp.

## Bounded manifest cardinality

To avoid unbounded work on malformed or adversarial caller-supplied records,
the validator now rejects more than **20,000 declared exchange sessions** or
**256 historical ticker periods per stable asset ID** with `MANIFEST_TOO_LARGE`.
These are engineering resource limits, **not proof of exchange-calendar
completeness, venue authenticity, ticker availability, or data licensing**.
They are checked before iterating the respective records and before
constructing the canonical JSON hash. The existing checks still reject
session/bar length disagreement, duplicate dates, invalid symbol intervals
and unreliable chronology. Larger legitimate research histories require a
separately designed, independently validated chunking protocol; do not simply
increase these caps for a specific untrusted file.

## Historical ticker compatibility with the draft paper ledger

The research manifest intentionally accepts **1–15-character** historical
ticker identifiers. The draft paper scanner, risk guard and SQLite ledger
accept at most **10 characters** and have a separate, more restrictive
contract. A long historical ticker is still legitimate evidence and must
never be silently truncated into a different paper symbol.

After all provenance claims are validated, `ManifestReceipt` now exposes:
`paper_symbol` (a current as-of ticker only if representable) and
`paper_symbol_reason`. An 11–15-character current identifier yields
`paper_symbol=None` and `PAPER_SYMBOL_UNSUPPORTED_FORMAT`, even though
the research receipt and its original SHA-256 remain valid. Excluded or
already-delisted instruments also produce no paper symbol. Compatible
current symbols have reason `PAPER_SYMBOL_REPRESENTABLE_ONLY`.

These fields represent **syntax and current-universe compatibility ONLY**.
They are **not** a trade, scanner or paper-order eligibility verdict,
independent vendor authenticity, current market listing verification or
approval to connect a broker. Each historical bar's prior symbol still
requires a reviewed point-in-time mapping before replay. A downstream
paper path must reject a `None` paper symbol rather than substitute
another identifier. This is a cross-PR data/risk contract gate, not
integrated executable handling; obtain independent review and combined
tests alongside risk PR #23 and later ledger work.

**Limitations / integration gates:** This module neither opens artifact bytes nor verifies vendor delivery/authenticity, licensing grants, historical completeness or the calendar source's correctness. External expected hashes are operator-provided and must come from off-repo receipts; matching them only detects disagreement in claims. Caller-attested universe membership may be biased by survivorship, and bar-level first-seen / next-open revision timing is not checked here (see `alphora.research.data_provenance` and draft PR #17). There is no split/dividend cash or share accounting, total-return benchmark, real LEAN engine run, out-of-sample ML performance validation, or transaction simulator integration. No local dataset or provider token belongs in this public repository. Future review must test delistings, symbol reuse, true vendor revisions, exchange holidays, halts and corporate-action cash/share conservation using licensed off-repo data.

Run tests offline using `python -m unittest tests/test_market_manifest.py -v` from a checkout or `python -m unittest discover -s tests -v`. This manifest cannot authorize execution, data download, connectivity, orders, deployment or a performance claim. Alpaca remains entirely disconnected. The approved virtual-equity and risk limits are not enforced by this manifest.
