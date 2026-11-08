"""Restart, conflict and corruption tests for offline checkpoint acknowledgments."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alphora.offline_monitor import ET, MonitorCheckpoint, VerifiedSession, plan_monitor_tick
from alphora.offline_monitor_checkpoint import OfflineMonitorCheckpoints

DAY = date(2026, 11, 9)


def instant(hour=4, minute=0, second=0, *, day=DAY):
    return datetime.combine(day, time(hour, minute, second), tzinfo=ET)


def verified(*, day=DAY, early=False, halted=False, valid=True):
    return VerifiedSession(day, instant(9, 30, day=day),
                           instant(13 if early else 16, day=day),
                           valid, halted)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "monitor.db"
        self.db = OfflineMonitorCheckpoints(self.path)
        self.addCleanup(self.db.close)

    def ack(self, *, now=None, session=None, expected=None, completed=None, store=None):
        return (store or self.db).record_completed(
            now=now or instant(), session=session or verified(),
            expected=expected if expected is not None else self.db.load(DAY),
            completed=completed if completed is not None else frozenset({"positions"}),
        )

    def test_restart_retains_only_explicitly_completed_work(self):
        empty = self.db.load(DAY)
        self.assertEqual(empty, MonitorCheckpoint(DAY))
        first = self.ack(expected=empty)
        self.assertEqual(first.position_at, instant().astimezone(timezone.utc))
        self.assertIsNone(first.discovery_at)
        self.db.close()
        self.db = OfflineMonitorCheckpoints(self.path)
        self.assertEqual(self.db.load(DAY), first)
        at_four_seconds = plan_monitor_tick(
            now=instant(4, 0, 4), session=verified(),
            checkpoint=self.db.load(DAY))
        self.assertFalse(at_four_seconds.positions_due)
        self.assertTrue(at_four_seconds.discovery_due)
        at_five_seconds = plan_monitor_tick(
            now=instant(4, 0, 5), session=verified(),
            checkpoint=self.db.load(DAY))
        self.assertTrue(at_five_seconds.positions_due)
        # Only watchlist completion cannot credit unrelated work.
        updated = self.ack(now=instant(4, 0, 5),
                           expected=first, completed=frozenset({"watchlist"}))
        self.assertEqual(updated.position_at, first.position_at)
        self.assertIsNone(updated.discovery_at)
        self.assertIsNone(updated.news_at)

    def test_cas_prevents_repeated_or_concurrent_credits(self):
        other = OfflineMonitorCheckpoints(self.path)
        self.addCleanup(other.close)
        shared = self.db.load(DAY)
        self.ack(expected=shared)
        with self.assertRaisesRegex(ValueError, "MONITOR_CHECKPOINT_CONFLICT"):
            self.ack(expected=shared, store=other)
        self.assertEqual(self.db.load(DAY).position_at,
                         instant().astimezone(timezone.utc))
        with self.assertRaisesRegex(ValueError, "MONITOR_TASK_NOT_DUE"):
            self.ack(now=instant(4, 0, 1), expected=self.db.load(DAY))
        self.assertEqual(self.db.load(DAY).position_at,
                         instant().astimezone(timezone.utc))

    def test_rollover_does_not_reuse_old_session_timestamps(self):
        self.ack()
        next_day = date(2026, 11, 10)
        fresh = self.db.load(next_day)
        self.assertEqual(fresh, MonitorCheckpoint(next_day))
        done = self.ack(now=instant(day=next_day),
                        session=verified(day=next_day), expected=fresh)
        self.assertEqual(done.day, next_day)
        self.assertIsNotNone(self.db.load(DAY).position_at)
        self.assertIsNotNone(self.db.load(next_day).position_at)
        with self.assertRaisesRegex(ValueError, "INVALID_MONITOR_ACK"):
            self.ack(now=instant(day=next_day), session=verified(day=next_day),
                     expected=self.db.load(DAY))

    def test_halts_closed_premarket_and_early_close(self):
        before = self.db.load(DAY)
        # Premarket completion permitted for read-only monitoring.
        self.ack(now=instant(), expected=before)
        state = self.db.load(DAY)
        for bad_now, s in (
            (instant(3, 59), verified()),
            (instant(16), verified()),
            (instant(13), verified(early=True)),
            (instant(11), verified(halted=True)),
            (instant(11), verified(valid=False)),
        ):
            with self.subTest(time=bad_now, session=s):
                with self.assertRaisesRegex(ValueError, "MONITOR_SESSION_NOT_ACTIVE"):
                    self.ack(now=bad_now, session=s, expected=state,
                             completed=frozenset({"news"}))
                self.assertEqual(self.db.load(DAY), state)
        regular = self.ack(now=instant(12, 59), session=verified(early=True),
                           expected=state, completed=frozenset({"discovery"}))
        self.assertIsNotNone(regular.discovery_at)

    def test_invalid_ack_future_or_naive_clock_never_mutates(self):
        empty = self.db.load(DAY)
        for value in (
            frozenset(), frozenset({"unknown"}), {"positions"},
            frozenset({"positions", "invalid"}),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "INVALID_MONITOR_ACK"):
                self.ack(expected=empty, completed=value)
        for now in (datetime(2026, 11, 9, 10), instant(16), instant(3)):
            with self.subTest(now=now), self.assertRaisesRegex(
                    ValueError, "MONITOR_SESSION_NOT_ACTIVE"):
                self.ack(now=now, expected=empty)
        self.assertEqual(self.db.load(DAY), empty)

    def test_corrupt_persisted_time_and_future_cursor_fail_closed(self):
        self.ack()
        with sqlite3.connect(self.path) as raw:
            raw.execute("UPDATE monitor_checkpoints SET position_at=?",
                        ("not-a-utc-time",))
        with self.assertRaisesRegex(ValueError, "MONITOR_CHECKPOINT_CORRUPT"):
            self.db.load(DAY)
        with sqlite3.connect(self.path) as raw:
            raw.execute("UPDATE monitor_checkpoints SET position_at=?",
                        (instant(10).astimezone(timezone.utc).isoformat(),))
        # Valid UTC encoding but future timestamp must prevent retrograde ack.
        future = self.db.load(DAY)
        with self.assertRaisesRegex(ValueError, "MONITOR_SESSION_NOT_ACTIVE"):
            self.ack(now=instant(9), expected=future)
        self.assertEqual(self.db.load(DAY), future)

    def test_deterministic_daylight_saving_utc_persistence(self):
        summer = date(2026, 7, 6)
        t = instant(4, day=summer)
        current = self.db.load(summer)
        out = self.ack(now=t, session=verified(day=summer),
                       expected=current, completed=frozenset({"news"}))
        self.assertEqual(out.news_at, t.astimezone(timezone.utc))
        self.assertEqual(self.db.load(summer), out)
        self.assertEqual(out.news_at.hour, 8)  # 04:00 EDT == 08:00 UTC

    def test_sqlite_write_failure_rolls_back_all_tasks(self):
        with sqlite3.connect(self.path) as raw:
            raw.execute("""CREATE TRIGGER deny_ack BEFORE INSERT ON monitor_checkpoints
                           BEGIN SELECT RAISE(ABORT, 'forced_ack_failure'); END""")
        initial = self.db.load(DAY)
        with self.assertRaises(sqlite3.DatabaseError):
            self.ack(expected=initial,
                     completed=frozenset({"positions", "watchlist"}))
        self.assertEqual(self.db.load(DAY), initial)


if __name__ == "__main__":
    unittest.main()
