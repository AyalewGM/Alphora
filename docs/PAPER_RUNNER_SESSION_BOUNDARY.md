# Offline runner session boundary (issue #42)

**Status: safety containment only.** The local `PaperSessionRunner` is not accepted for multi-day, unattended or broker-connected trading. No Alpaca, network access, broker credentials or orders are involved.

## Risk being contained

`OfflineReservations` pins `session_id` and the account's `session_open_equity` to the original **$100,000** synthetic baseline. Its durable 2% loss observer is valid only for that pinned single session. Previously, `PaperSessionRunner.run()` accepted day 2 and later days while reusing the original loss baseline and book session identifier. For example, after a hypothetical prior close of $105,000, a $102,000 equity mark represents a 2.857% single-day decline; the original $100,000-based loss check could miss it. This is code-path arithmetic, not a verified authentic market replay.

## Bounded fail-closed behavior

Until there is an independently accepted durable session rollover, `run()` rejects:

- any supplied `SessionDay.day` different from the *persisted* book `session_id` (`SESSION_ROLLOVER_UNSUPPORTED`), including the first invocation;
- any batch containing more than one `SessionDay` **before processing the first one**, so a later unsupported session cannot leave a partial earlier batch.

An empty session batch is rejected with `MISSING_SESSION_MARK` rather than reporting the original $100,000 as a verified current equity mark when no price observations were supplied.

The batch preflight consumes at most two elements from an input iterable; an unbounded iterator cannot cause an unbounded in-memory batch. A single-day mark timestamp must also resolve to that session's calendar date in `America/New_York` (not merely match a forged bar/day label); this coarse consistency check is **not** an authoritative trading-hours or exchange-calendar validation. Inputs supplied by a generator may have side effects outside this runner when iterated.

A valid one-day call keeps the existing paper-only model and original risk settings: $100,000 virtual initial equity, 0.5% planned per-trade risk, 10% instrument allocation limit, 2% loss halt relative to this *one* session's starting equity, and a daily rebalance requirement that is not yet implemented as a safe recurrent feature. Inputs remain caller-asserted, not exchange-certified.

Because the existing paper venue quantizes submitted prices to cents but the runner marks holdings using the caller's unrounded Decimal, a fractional-cent executable mark could make the venue valuation, risk observation and reported equity disagree. The runner now rejects non-cent-exact, non-finite or nonpositive selected marks during preflight (`INEXACT_PAPER_MARK`/`INVALID_MARKS`), before any book/venue mutation. This is a deliberately narrow **offline paper-venue** compatibility rule; it is not a claim that all US equity/ETF real-market quotes have only two decimal places. Regulated tick sizes and genuine sub-cent instruments require a separately validated pricing/rounding policy before admission. A rejected mark can be corrected and resubmitted without consuming the in-memory session guard.

The runner's marked-equity calculation now uses an isolated 50-digit Decimal context with an `Inexact` trap, rather than the caller's current Decimal precision. An otherwise valid $98,002 mark must not round to the $98,000 (2% loss) halt boundary when a caller uses precision 3. Overflow or inexact accumulation fails closed as `INEXACT_PAPER_ACCOUNT`. Synthetic tests cover precision 3, 6, 28 and 80, plus unrepresentable extremes. This is a **runner valuation** correction; it does not establish context independence throughout the venue/ledger or validate multi-day session baselines.

The final session report recomputes equity from the settled book and same-session marks **after simulated fills and fees**, then repeats the one-way daily-loss observation before reporting. This prevents a pre-entry $100,000 mark from being misreported after a $1 fee reduces settled equity to $99,999. It does not establish authentic quotes, crash-safe replay, or independent reconciliation of an external venue.

Test regressions cover nonmutation of book, ledger and paper adapter on rejected cross-session or empty inputs, subsequent same-session reuse after failed preflight, a missing mark for an existing position, and post-fill fee equity reporting. These are synthetic tests, not an independent financial QA verdict.

## Strict caller-signal preflight (stacked safety follow-up)

Before any paper-venue advance, reconciliation, or order mutation, the
runner validates the session signal mapping, each typed `SessionSignal`,
and any explicitly provided stop price. An explicit zero, non-finite,
wrong-typed, or non-protective stop is rejected as
`INVALID_SESSION_SIGNAL`, rather than silently falling back to a default
stop through a false-valued expression. This check occurs after executable
mark validation and before the in-memory session is consumed. Synthetic
negative tests verify no changes to the book, ledger or venue and permit a
subsequent valid first attempt.

A nonempty session batch is not sufficient evidence of a current price:
a supplied day with an empty bars mapping previously produced a synthetic
$100,000 final-equity report without an executable mark. The preflight now
rejects such input as `MISSING_SESSION_MARK`, and rejects non-mapping
bar containers as `INVALID_MARKS` before processing any orders. This
is a **presence/structure check only**; it does not authenticate the marks
or establish exchange trading-session hours. Regression tests verify that
rejection leaves venue, ledger, book and in-memory session state unchanged,
and that a corrected valid day remains usable.

The same preflight now rejects malformed ticker identifiers in both the
asserted mark mapping and signal mapping, and rejects non-string scanner and
catalyst metadata. These are validated against the paper venue's bounded
uppercase ticker contract before the runner consumes its in-memory session
or advances an existing paper order. An invalid secondary signal must not
permit another valid ticker's buy to proceed. Regression tests cover mixed
valid/malformed inputs, unchanged book/ledger/venue, and successful retry.
Unrecognized but well-typed classification strings remain non-eligible
signals rather than authentication or eligibility proofs.

This does not authenticate signal origins, timestamps, catalyst assertions,
or quoted market prices, and does not repair restart/session-rollover
persistence. This change is developed as a separate stacked draft PR on top
of #43 so its previously approved exact head remains unchanged.

## Remaining release blockers

A safe multi-day implementation must persist authoritative exchange-session identity and the verified opening portfolio mark for each session, reconcile cash and held/pending orders, handle fees and corporate actions, and make rollover crash-safe and idempotent. Re-entry at the same session on runner restart still requires a durable progress/checkpoint gate; the in-memory `_last_day` is not restart protection. Batch preflight is not a transaction or atomic portfolio replay within the accepted single session. Quote authenticity, stale marks, overnight gaps, session calendar, full reconciliation, restart recovery, and qualified independent financial review are not established by this containment fix.

**No unattended paper execution; no live brokerage integration or activation.** Keep PR in draft until independent code and financial risk reviewers approve current head and CI evidence.

## Cent-executable protective-stop risk calculation (stacked follow-up)

The offline paper venue accepts cent-quantized execution marks, so a default
protective stop calculated as `mark * (1 - stop_fraction)` can otherwise
produce a non-executable fractional-cent threshold. Treating that theoretical
price as an available protective execution level understates planned
per-share loss for low-priced shares and can produce an oversized paper
position. Example: a $0.02 mark and 4% default stop imply a theoretical
$0.0192; the next lower executable cent tick is **$0.01**. The runner
now floors the **implicit** stop to the next lower cent tick *before* the
decision/risk sizing; on the approved $100,000 starting equity and 0.5%
planned risk limit, this constrains the example to at most 50,000 shares
($500 planned stop loss), before other limits and fees. If no strictly
positive cent-priced stop exists (e.g. a $0.01 mark), preflight fails
closed with `UNREPRESENTABLE_PAPER_STOP` before venue/book mutations.
An **explicit** fractional-cent stop is rejected with
`INEXACT_PAPER_STOP` instead of silently modifying its meaning.

These are **synthetic tick-arithmetic and intent-sizing** protections, not
a guarantee that a protective sell will fill at its stop. Gap-through-stop
losses, unavailable quotes, illiquidity, slippage, single-session restart
replay, missing licensed point-in-time marks, and multi-day rollover remain
separate, blocking financial and release acceptance. No brokerage connection,
credentials, deployment, unattended execution or real-money trading is
introduced. The approved paper risk settings are unchanged.
