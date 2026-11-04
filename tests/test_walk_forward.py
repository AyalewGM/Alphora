"""Synthetic session-index tests; no market data, credentials or order execution."""
import unittest
from datetime import date, datetime, timedelta

from alphora.research.walk_forward import (rolling_purged_folds, summarize_walk_forward_coverage)


def fake_sessions(n):
    result = []
    current = date(2026, 1, 1)
    while len(result) < n:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


class WalkForwardFoldTests(unittest.TestCase):
    def test_exact_nonoverlapping_oos_and_matured_labels(self):
        days = fake_sessions(20)
        folds = rolling_purged_folds(
            days, train_sessions=6, test_sessions=3,
            label_horizon_sessions=2, embargo_sessions=1)
        self.assertEqual(len(folds), 3)
        self.assertEqual([(f.train_start, f.train_stop, f.test_start, f.test_stop)
                          for f in folds],
                         [(0, 6, 9, 12), (3, 9, 12, 15), (6, 12, 15, 18)])
        for index, fold in enumerate(folds):
            self.assertEqual(fold.ordinal, index)
            self.assertEqual(fold.last_train_label_index, fold.train_stop + 1)
            self.assertEqual(fold.last_test_label_index, fold.test_stop + 1)
            self.assertLess(fold.last_train_label_index + 1, fold.test_start)
            self.assertLess(fold.last_test_label_index, len(days))
            self.assertEqual(fold.train_first_day, days[fold.train_start])
            self.assertEqual(fold.train_last_day, days[fold.train_stop - 1])
            self.assertEqual(fold.test_first_day, days[fold.test_start])
            self.assertEqual(fold.test_last_day, days[fold.test_stop - 1])
            if index:
                self.assertEqual(folds[index - 1].test_stop, fold.test_start)

    def test_minimum_history_includes_mature_test_labels(self):
        config = dict(train_sessions=6, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1)
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_MATURE_HISTORY"):
            rolling_purged_folds(fake_sessions(13), **config)
        folds = rolling_purged_folds(fake_sessions(14), **config)
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0].last_test_label_index, 13)

    def test_no_embargo_still_purges_label_overlap(self):
        folds = rolling_purged_folds(
            fake_sessions(11), train_sessions=4, test_sessions=2,
            label_horizon_sessions=1)
        first = folds[0]
        self.assertEqual(first.train_stop, 4)
        self.assertEqual(first.last_train_label_index, 4)
        self.assertEqual(first.test_start, 5)
        self.assertEqual(first.last_test_label_index, 7)

    def test_reserved_final_holdout_excludes_test_label_outcomes(self):
        days = fake_sessions(32)
        held_out = 6
        folds = rolling_purged_folds(
            days, train_sessions=8, test_sessions=3,
            label_horizon_sessions=2, embargo_sessions=1,
            holdout_sessions=held_out)
        self.assertTrue(folds)
        boundary = len(days) - held_out
        for fold in folds:
            self.assertEqual(fold.holdout_start_index, boundary)
            self.assertLess(fold.test_stop, boundary)
            self.assertLess(fold.last_test_label_index, boundary)
            self.assertLess(fold.last_train_label_index, fold.test_start)
        # Labels for prior out-of-sample rows cannot enter the untouched tail.
        self.assertEqual(folds, rolling_purged_folds(
            list(days), train_sessions=8, test_sessions=3,
            label_horizon_sessions=2, embargo_sessions=1,
            holdout_sessions=held_out))

    def test_holdout_protection_removes_later_folds(self):
        days = fake_sessions(25)
        config = dict(train_sessions=6, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1)
        regular = rolling_purged_folds(days, **config)
        reserved = rolling_purged_folds(days, holdout_sessions=8, **config)
        self.assertGreater(len(regular), len(reserved))
        self.assertEqual(reserved[-1].holdout_start_index, 17)
        self.assertTrue(all(f.last_test_label_index < 17 for f in reserved))
        self.assertTrue(all(f.holdout_start_index == len(days) for f in regular))

    def test_holdout_needs_enough_mature_pre_holdout_history(self):
        config = dict(train_sessions=6, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1)
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_MATURE_HISTORY"):
            rolling_purged_folds(fake_sessions(16), holdout_sessions=3, **config)
        result = rolling_purged_folds(fake_sessions(17), holdout_sessions=3,
                                     **config)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].last_test_label_index, 13)
        self.assertEqual(result[0].holdout_start_index, 14)

    def test_invalid_holdout_configuration_fails_closed(self):
        for invalid in (-1, 1.0, True, "2", None):
            with self.subTest(holdout_sessions=invalid):
                with self.assertRaisesRegex(ValueError, "INVALID_FOLD_CONFIGURATION"):
                    rolling_purged_folds(fake_sessions(30),
                                         train_sessions=6, test_sessions=3,
                                         label_horizon_sessions=2,
                                         holdout_sessions=invalid)

    def test_sessions_must_be_exact_dates_strictly_increasing(self):
        days = fake_sessions(20)
        kwargs = dict(train_sessions=6, test_sessions=3, label_horizon_sessions=2)
        for bad in [None, (), list(reversed(days)), days[:3] + (days[2],) + days[4:],
                    days[:1] + (datetime(2026, 1, 2),) + days[2:],
                    ["2026-01-01"] + list(days[1:])]:
            with self.subTest(bad=repr(bad)[:100]):
                with self.assertRaisesRegex(ValueError, "INVALID_SESSION_SEQUENCE|INSUFFICIENT_MATURE_HISTORY"):
                    rolling_purged_folds(bad, **kwargs)

    def test_invalid_config_fails_closed_including_bool(self):
        days = fake_sessions(30)
        valid = dict(train_sessions=10, test_sessions=5,
                     label_horizon_sessions=2, embargo_sessions=0)
        for mutation in [{"train_sessions": 1}, {"train_sessions": True},
                         {"test_sessions": 0}, {"test_sessions": 2.5},
                         {"label_horizon_sessions": 0},
                         {"label_horizon_sessions": False},
                         {"embargo_sessions": -1}, {"embargo_sessions": True}]:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, "INVALID_FOLD_CONFIGURATION"):
                    rolling_purged_folds(days, **(valid | mutation))

    def test_repeated_runs_and_container_types_are_deterministic(self):
        days = fake_sessions(41)
        config = dict(train_sessions=12, test_sessions=4,
                      label_horizon_sessions=3, embargo_sessions=2)
        first = rolling_purged_folds(days, **config)
        self.assertEqual(first, rolling_purged_folds(list(days), **config))
        self.assertTrue(all(first[i].test_stop <= first[i+1].test_start
                            for i in range(len(first)-1)))
        # Public output does not expose a trading or order interface.
        self.assertFalse(hasattr(first[0], "submit_order"))

    def test_coverage_reports_all_unscored_dates_not_just_test_rows(self):
        days = fake_sessions(20)
        report = summarize_walk_forward_coverage(
            days, train_sessions=6, test_sessions=3,
            label_horizon_sessions=2, embargo_sessions=1)
        self.assertEqual(report.total_sessions, 20)
        self.assertEqual(report.holdout_start_index, 20)
        self.assertEqual(report.reserved_holdout_sessions, 0)
        self.assertEqual(report.folds, 3)
        self.assertEqual(report.tested_preholdout_sessions, 9)
        self.assertEqual(report.untested_preholdout_sessions, 11)
        self.assertEqual(report.first_test_index, 9)
        self.assertEqual(report.last_test_stop_index, 18)
        self.assertEqual(report.trailing_preholdout_sessions, 2)
        self.assertEqual(report.last_test_label_index, 19)
        self.assertEqual(report.tested_preholdout_sessions +
                         report.untested_preholdout_sessions,
                         report.holdout_start_index)

    def test_holdout_coverage_excludes_reserved_rows_and_label_tail(self):
        config = dict(train_sessions=8, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1,
                      holdout_sessions=6)
        report = summarize_walk_forward_coverage(fake_sessions(32), **config)
        self.assertEqual(report.holdout_start_index, 26)
        self.assertEqual(report.reserved_holdout_sessions, 6)
        self.assertEqual(report.folds, 4)
        self.assertEqual(report.tested_preholdout_sessions, 12)
        self.assertEqual(report.untested_preholdout_sessions, 14)
        self.assertEqual(report.trailing_preholdout_sessions, 3)
        self.assertEqual(report.last_test_label_index, 24)
        self.assertLess(report.last_test_label_index, report.holdout_start_index)
        self.assertEqual(report, summarize_walk_forward_coverage(
            list(fake_sessions(32)), **config))

    def test_coverage_fails_closed_using_same_fold_validation(self):
        config = dict(train_sessions=6, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1)
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_MATURE_HISTORY"):
            summarize_walk_forward_coverage(fake_sessions(13), **config)
        with self.assertRaisesRegex(ValueError, "INVALID_SESSION_SEQUENCE"):
            summarize_walk_forward_coverage(list(reversed(fake_sessions(20))), **config)
        with self.assertRaisesRegex(ValueError, "INVALID_FOLD_CONFIGURATION"):
            summarize_walk_forward_coverage(fake_sessions(20),
                                            **(config | {"test_sessions": True}))

    def test_coverage_counts_closed_forward_label_interval_overlap(self):
        # Test rows 9..17 have horizon-2 labels on closed intervals:
        # [9,11], [10,12], ... [17,19]. Only 3 can be disjoint.
        report = summarize_walk_forward_coverage(
            fake_sessions(20), train_sessions=6, test_sessions=3,
            label_horizon_sessions=2, embargo_sessions=1)
        self.assertEqual(report.tested_preholdout_sessions, 9)
        self.assertEqual(report.max_nonoverlapping_test_label_windows, 3)
        self.assertEqual(report.overlap_excluded_test_label_windows, 6)
        self.assertEqual(report.max_nonoverlapping_test_label_windows +
                         report.overlap_excluded_test_label_windows,
                         report.tested_preholdout_sessions)

    def test_shorter_label_horizon_changes_overlap_accounting(self):
        # Nine consecutive OOS rows for a one-session forward label
        # admit at most five disjoint closed index intervals.
        report = summarize_walk_forward_coverage(
            fake_sessions(20), train_sessions=6, test_sessions=3,
            label_horizon_sessions=1, embargo_sessions=1)
        self.assertEqual(report.tested_preholdout_sessions, 9)
        self.assertEqual(report.max_nonoverlapping_test_label_windows, 5)
        self.assertEqual(report.overlap_excluded_test_label_windows, 4)

    def test_holdout_excludes_reserved_intervals_from_overlap_counts(self):
        report = summarize_walk_forward_coverage(
            fake_sessions(32), train_sessions=8, test_sessions=3,
            label_horizon_sessions=2, embargo_sessions=1,
            holdout_sessions=6)
        self.assertEqual(report.tested_preholdout_sessions, 12)
        self.assertEqual(report.max_nonoverlapping_test_label_windows, 4)
        self.assertEqual(report.overlap_excluded_test_label_windows, 8)
        self.assertLess(report.last_test_label_index,
                        report.holdout_start_index)


    def test_input_fingerprint_binds_calendar_folds_and_parameters(self):
        days = fake_sessions(50)
        config = dict(train_sessions=8, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1,
                      holdout_sessions=6)
        base = summarize_walk_forward_coverage(days, **config)
        self.assertEqual(len(base.input_fingerprint_sha256), 64)
        self.assertFalse(base.expected_input_fingerprint_matched)
        self.assertEqual(base.input_fingerprint_sha256,
                         summarize_walk_forward_coverage(list(days), **config).input_fingerprint_sha256)
        earlier_first_day = (days[0] - timedelta(days=1),) + days[1:]
        self.assertNotEqual(base.input_fingerprint_sha256,
                            summarize_walk_forward_coverage(earlier_first_day, **config).input_fingerprint_sha256)
        self.assertNotEqual(base.input_fingerprint_sha256,
                            summarize_walk_forward_coverage(days,
                                **(config | {"label_horizon_sessions": 3})).input_fingerprint_sha256)
        self.assertNotEqual(base.input_fingerprint_sha256,
                            summarize_walk_forward_coverage(days,
                                **(config | {"holdout_sessions": 5})).input_fingerprint_sha256)

    def test_input_fingerprint_includes_reserved_tail_date_changes(self):
        days = fake_sessions(50)
        config = dict(train_sessions=8, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1,
                      holdout_sessions=6)
        old = summarize_walk_forward_coverage(days, **config)
        changed_tail = days[:-1] + (days[-1] + timedelta(days=1),)
        new = summarize_walk_forward_coverage(changed_tail, **config)
        self.assertEqual(old.folds, new.folds)
        self.assertEqual(old.tested_preholdout_sessions, new.tested_preholdout_sessions)
        self.assertNotEqual(old.input_fingerprint_sha256, new.input_fingerprint_sha256)

    def test_expected_input_fingerprint_fails_closed(self):
        days = fake_sessions(50)
        config = dict(train_sessions=8, test_sessions=3,
                      label_horizon_sessions=2, embargo_sessions=1,
                      holdout_sessions=6)
        base = summarize_walk_forward_coverage(days, **config)
        pinned = summarize_walk_forward_coverage(
            list(days), expected_input_fingerprint_sha256=base.input_fingerprint_sha256,
            **config)
        self.assertTrue(pinned.expected_input_fingerprint_matched)
        self.assertEqual(base.input_fingerprint_sha256, pinned.input_fingerprint_sha256)
        for invalid in (True, 123, "bad", "A" * 64):
            with self.subTest(expected=invalid):
                with self.assertRaisesRegex(ValueError, "INVALID_WALK_FORWARD_FINGERPRINT"):
                    summarize_walk_forward_coverage(
                        days, expected_input_fingerprint_sha256=invalid, **config)
        with self.assertRaisesRegex(ValueError, "WALK_FORWARD_FINGERPRINT_MISMATCH"):
            summarize_walk_forward_coverage(
                days, expected_input_fingerprint_sha256="0" * 64, **config)


if __name__ == "__main__":
    unittest.main()
