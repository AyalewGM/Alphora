"""Synthetic session-index tests; no market data, credentials or order execution."""
import unittest
from datetime import date, datetime, timedelta

from alphora.research.walk_forward import rolling_purged_folds


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


if __name__ == "__main__":
    unittest.main()
