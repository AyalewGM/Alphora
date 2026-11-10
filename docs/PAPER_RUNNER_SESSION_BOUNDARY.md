# Offline runner session boundary (issue #42)

**Status: safety containment only.** The local `PaperSessionRunner` is not accepted for multi-day, unattended or broker-connected trading. No Alpaca, network access, broker credentials or orders are involved.

## Risk being contained

`OfflineReservations` pins `session_id` and the account's `session_open_equity` to the original **$100,000** synthetic baseline. Its durable 2% loss observer is valid only for that pinned single session. Previously, `PaperSessionRunner.run()` accepted day 2 and later days while reusing the original loss baseline and book session identifier. For example, after a hypothetical prior close of $105,000, a $102,000 equity mark represents a 2.857% single-day decline; the original $100,000-based loss check could miss it. This is code-path arithmetic, not a verified authentic market replay.

## Bounded fail-closed behavior

Until there is an independently accepted durable session rollover, `run()` rejects:

- any supplied `SessionDay.day` different from the *persisted* book `session_id` (`SESSION_ROLLOVER_UNSUPPORTED`), including the first invocation;
- any batch containing more than one `SessionDay` **before processing the first one**, so a later unsupported session cannot leave a partial earlier batch.

The batch preflight consumes at most two elements from an input iterable; an unbounded iterator cannot cause an unbounded in-memory batch. A single-day mark timestamp must also resolve to that session's calendar date in `America/New_York` (not merely match a forged bar/day label); this coarse consistency check is **not** an authoritative trading-hours or exchange-calendar validation. Inputs supplied by a generator may have side effects outside this runner when iterated.

A valid one-day call keeps the existing paper-only model and original risk settings: $100,000 virtual initial equity, 0.5% planned per-trade risk, 10% instrument allocation limit, 2% loss halt relative to this *one* session's starting equity, and a daily rebalance requirement that is not yet implemented as a safe recurrent feature. Inputs remain caller-asserted, not exchange-certified.

Test regressions cover nonmutation of book, ledger and paper adapter on rejected cross-session inputs, subsequent same-session reuse after failed preflight, and a missing mark for an existing position. These are synthetic tests, not an independent financial QA verdict.

## Remaining release blockers

A safe multi-day implementation must persist authoritative exchange-session identity and the verified opening portfolio mark for each session, reconcile cash and held/pending orders, handle fees and corporate actions, and make rollover crash-safe and idempotent. Re-entry at the same session on runner restart still requires a durable progress/checkpoint gate; the in-memory `_last_day` is not restart protection. Batch preflight is not a transaction or atomic portfolio replay within the accepted single session. Quote authenticity, stale marks, overnight gaps, session calendar, full reconciliation, restart recovery, and qualified independent financial review are not established by this containment fix.

**No unattended paper execution; no live brokerage integration or activation.** Keep PR in draft until independent code and financial risk reviewers approve current head and CI evidence.
