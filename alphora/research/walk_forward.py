"""Deterministic rolling, purged walk-forward session indices for offline research.

Pure date/index partitioning, not a data-integrity or backtest certification.
No vendor data, ML dependencies, broker endpoints, orders, or network calls.
"""
from dataclasses import dataclass
from datetime import date
from typing import Sequence


@dataclass(frozen=True)
class WalkForwardFold:
    """Half-open index windows. Labels mature at index + label_horizon_sessions."""

    ordinal: int
    train_start: int
    train_stop: int
    test_start: int
    test_stop: int
    last_train_label_index: int
    last_test_label_index: int
    train_first_day: date
    train_last_day: date
    test_first_day: date
    test_last_day: date
    holdout_start_index: int  # No research test labels reach this boundary.


@dataclass(frozen=True)
class WalkForwardCoverage:
    """Counts only candidate research test rows; NOT independent trials.

    Untested pre-holdout rows include training, purge/embargo gaps and
    label-maturation tail sessions. Nothing reports realized returns.
    """

    total_sessions: int
    holdout_start_index: int
    reserved_holdout_sessions: int
    folds: int
    tested_preholdout_sessions: int
    untested_preholdout_sessions: int
    first_test_index: int
    last_test_stop_index: int
    trailing_preholdout_sessions: int
    last_test_label_index: int
    # Maximum disjoint closed [sample_index, sample_index + horizon] target
    # intervals. This is NOT an estimate of statistically independent trials.
    max_nonoverlapping_test_label_windows: int
    overlap_excluded_test_label_windows: int


def rolling_purged_folds(
    sessions: Sequence[date],
    *,
    train_sessions: int,
    test_sessions: int,
    label_horizon_sessions: int,
    embargo_sessions: int = 0,
    holdout_sessions: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Produce equal-length, non-overlapping out-of-sample test windows.

    Sessions must be externally vetted actual sessions, strictly increasing and
    complete. Every sample at index i requires labels extending to index i+h.
    For each fold, train sample labels mature strictly *before* the test window
    begins, plus an optional additional embargo of completed sessions.

    Later folds intentionally retrain using prior matured OOS history. All test
    labels must themselves have matured within the input sequence. No partial
    final fold is emitted, so the caller must report excluded trailing sessions.
    Optional final holdout reserves the most recent N samples and their outcomes
    exclusively for separate untouched evaluation. Training/test labels cannot
    mature inside that reserved tail; it does not train or score the holdout.
    This does NOT prevent feature-level lookahead or establish vendor truth.
    """
    if (type(train_sessions) is not int or train_sessions < 2
            or type(test_sessions) is not int or test_sessions < 1
            or type(label_horizon_sessions) is not int
            or label_horizon_sessions < 1
            or type(embargo_sessions) is not int or embargo_sessions < 0
            or type(holdout_sessions) is not int or holdout_sessions < 0):
        raise ValueError("INVALID_FOLD_CONFIGURATION")
    if (not isinstance(sessions, (tuple, list)) or
            not all(type(day) is date for day in sessions) or
            any(later <= earlier for earlier, later in zip(sessions, sessions[1:]))):
        raise ValueError("INVALID_SESSION_SEQUENCE")

    n = len(sessions)
    holdout_start = n - holdout_sessions
    gap = label_horizon_sessions + embargo_sessions
    # Last OOS row also requires its future-horizon label to be observed.
    required = train_sessions + gap + test_sessions + label_horizon_sessions
    if holdout_start < required:
        raise ValueError("INSUFFICIENT_MATURE_HISTORY")

    folds = []
    offset = 0
    while True:
        train_start = offset
        train_stop = train_start + train_sessions
        test_start = train_stop + gap
        test_stop = test_start + test_sessions
        last_train_label = train_stop - 1 + label_horizon_sessions
        last_test_label = test_stop - 1 + label_horizon_sessions

        if last_test_label >= holdout_start:
            break
        # Defense-in-depth; the test date must be after every training label.
        if last_train_label + embargo_sessions >= test_start:
            raise ValueError("PURGE_INVARIANT_VIOLATED")
        folds.append(WalkForwardFold(
            ordinal=len(folds),
            train_start=train_start,
            train_stop=train_stop,
            test_start=test_start,
            test_stop=test_stop,
            last_train_label_index=last_train_label,
            last_test_label_index=last_test_label,
            train_first_day=sessions[train_start],
            train_last_day=sessions[train_stop - 1],
            test_first_day=sessions[test_start],
            test_last_day=sessions[test_stop - 1],
            holdout_start_index=holdout_start,
        ))
        # Step exactly one test window to avoid counting an OOS date twice.
        offset += test_sessions

    return tuple(folds)


def summarize_walk_forward_coverage(
    sessions: Sequence[date], *,
    train_sessions: int,
    test_sessions: int,
    label_horizon_sessions: int,
    embargo_sessions: int = 0,
    holdout_sessions: int = 0,
) -> WalkForwardCoverage:
    """Return auditable index counts from the verified fold generator.

    Do not interpret a large raw session count as independent performance
    observations: folds retrain and adjacent forward-return labels overlap.
    This function does no fitting, scoring, data fetching or execution.
    """
    folds = rolling_purged_folds(
        sessions, train_sessions=train_sessions, test_sessions=test_sessions,
        label_horizon_sessions=label_horizon_sessions,
        embargo_sessions=embargo_sessions,
        holdout_sessions=holdout_sessions,
    )
    boundary = folds[0].holdout_start_index
    tested = sum(f.test_stop - f.test_start for f in folds)
    # Forward return labels share prices whenever [i, i+h] intervals overlap.
    # Greedy earliest-finish selection is optimal for equal-length intervals:
    # it gives the MAXIMUM count of pairwise nonoverlapping target intervals,
    # not an independently sampled or statistically effective sample size.
    max_nonoverlapping = 0
    last_label_end = -1
    for fold in folds:
        for index in range(fold.test_start, fold.test_stop):
            if index > last_label_end:  # Closed intervals: no shared endpoint.
                max_nonoverlapping += 1
                last_label_end = index + label_horizon_sessions
    # The generator returns contiguous, non-overlapping test spans.
    if (any(f.holdout_start_index != boundary for f in folds)
            or any(a.test_stop != b.test_start for a, b in zip(folds, folds[1:]))
            or folds[-1].last_test_label_index >= boundary
            or tested > boundary
            or not (0 < max_nonoverlapping <= tested)):
        raise ValueError("INVALID_FOLD_COVERAGE")
    return WalkForwardCoverage(
        total_sessions=len(sessions),
        holdout_start_index=boundary,
        reserved_holdout_sessions=holdout_sessions,
        folds=len(folds),
        tested_preholdout_sessions=tested,
        untested_preholdout_sessions=boundary - tested,
        first_test_index=folds[0].test_start,
        last_test_stop_index=folds[-1].test_stop,
        trailing_preholdout_sessions=boundary - folds[-1].test_stop,
        last_test_label_index=folds[-1].last_test_label_index,
        max_nonoverlapping_test_label_windows=max_nonoverlapping,
        overlap_excluded_test_label_windows=tested - max_nonoverlapping,
    )
