# Offline manifest/calendar cross-receipt guard (issue #20)

This experimental research-only function connects **two already-existing**
components: `alphora.research.market_manifest` and
`alphora.research.session_calendar`. Previously a caller could independently
accept both receipts without establishing that they described **the same
exchange dates, venue, revision and session hours**. The new function
`bind_manifest_calendar(...)` checks that relationship before downstream
research treats the paired receipts as internally consistent.

The binding revalidates both receipts, checks separately supplied expected
artifact/calendar/manifest hashes supplied as three required separate pins,
compares source/version,
venue and exact first/last date, and rejects missing, extra, reordered or
inconsistent exchange sessions. It also recomputes derived paper-symbol
representability metadata and rejects a receipt with altered symbol/status
fields even if its embedded manifest content hash is unchanged. It compares regular/early-close hours
**as instants**, not as a naive local clock; it rejects information claimed to
have arrived after the manifest's calendar cutoff and calendar snapshots with
a later `as_of` than **either** the market snapshot or the claimed
pre-session calendar cutoff. A calendar schedule can be published before its
last covered exchange date; the schedule inspector accepts a prospective
`as_of` only when all day-level information claims are no later than the
snapshot. Retrospective calendar revisions cannot be reclassified as
pre-session evidence merely because their day records claim earlier
availability. Because the market manifest
cannot represent an intraday venue halt, it rejects any calendar with
`venue_halts` rather than silently dropping that risk. It returns a
deterministic binding SHA-256 for a local receipt.

Example, assuming separately validated and pinned offline inputs:

```python
from alphora.research.manifest_calendar_binding import bind_manifest_calendar

binding = bind_manifest_calendar(
    market_receipt,
    calendar_receipt,
    expected_artifact_sha256=external_artifact_digest,
    expected_calendar_sha256=external_calendar_digest,
    expected_manifest_sha256=external_manifest_digest,
)
```

**Evidence status:** This is *caller-attested structural consistency*, not
vendor authentication. A matching SHA-256 from the same operator cannot
certify data origin, licensing, exchange holidays, delivery timestamps,
historical constituent membership, corporate actions or actual market
performance. Real provider data must stay off GitHub; source authority
requires a separate independent review. No files are opened and no market,
broker or trading network is connected. The result is **never** paper-order
eligibility or risk-policy authority.

`expected_manifest_sha256` is **mandatory**: a recomputed manifest digest
embedded in the supplied receipt is not an independent acceptance pin. An
omitted, malformed or different expected manifest hash is rejected before
returning a binding. The test fixture self-pins its synthetic input for
convenience; this is explicitly **not** authentic external provenance.

All fixtures in `tests/test_manifest_calendar_binding.py` are synthetic.
Required executable checks (currently unverified because GitHub Actions
cannot start under #71):

```bash
python -m unittest discover -s tests -p 'test_manifest_calendar_binding.py' -v
python -m unittest discover -s tests -v
```

Obtain raw-head and combined GitHub Actions test receipts, independent data
and financial-correctness review, and explicit release authorization before
claiming this research milestone accepted. Do not merge automatically.
