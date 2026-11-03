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


def rolling_purged_folds(
    sessions: Sequence[date],
    *,
    train_sessions: int,
    test_sessions: int,
    label_horizon_sessions: int,
    embargo_sessions: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Produce equal-length, non-overlapping out-of-sample test windows.

    Sessions must be externally vetted actual sessions, strictly increasing and
    complete. Every sample at index i requires labels extending to index i+h.
    For each fold, train sample labels mature strictly *before* the test window
    begins, plus an optional additional embargo of completed sessions.

    Later folds intentionally retrain using prior matured OOS history. All test
    labels must themselves have matured within the input sequence. No partial
    final fold is emitted, so the caller must report excluded trailing sessions.
    This does NOT prevent feature-level lookahead or establish vendor truth.
    """
    if (type(train_sessions) is not int or train_sessions < 2
            or type(test_sessions) is not int or test_sessions < 1
            or type(label_horizon_sessions) is not int
            or label_horizon_sessions < 1
            or type(embargo_sessions) is not int or embargo_sessions < 0):
        raise ValueError("INVALID_FOLD_CONFIGURATION")
    if (not isinstance(sessions, (tuple, list)) or
            not all(type(day) is date for day in sessions) or
            any(later <= earlier for earlier, later in zip(sessions, sessions[1:]))):
        raise ValueError("INVALID_SESSION_SEQUENCE")

    n = len(sessions)
    gap = label_horizon_sessions + embargo_sessions
    # Last OOS row also requires its future-horizon label to be observed.
    required = train_sessions + gap + test_sessions + label_horizon_sessions
    if n < required:
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

        if last_test_label >= n:
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
        ))
        # Step exactly one test window to avoid counting an OOS date twice.
        offset += test_sessions

    return tuple(folds)
