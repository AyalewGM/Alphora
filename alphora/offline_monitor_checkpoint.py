"""Durable completion cursors for the *offline* session-monitor planner.

No scheduling loop, source feed, account/portfolio state, order or broker access.
Only persist work that a caller explicitly reports as completed. A cursor is
NOT proof that external work happened, and is NOT the daily-loss kill switch.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

from .offline_monitor import (
    ET, MonitorCheckpoint, VerifiedSession, plan_monitor_tick,
)

_TASKS = {
    "positions": ("position_at", "positions_due"),
    "watchlist": ("watchlist_at", "watchlist_due"),
    "discovery": ("discovery_at", "discovery_due"),
    "news": ("news_at", "news_due"),
}
_COLUMNS = ("position_at", "watchlist_at", "discovery_at", "news_at")


def _day(day):
    if type(day) is not date:
        raise ValueError("INVALID_MONITOR_DAY")
    return day.isoformat()


def _decode(day, values):
    try:
        timestamps = []
        for value in values:
            if value is None:
                timestamps.append(None)
                continue
            if type(value) is not str:
                raise ValueError("bad type")
            stamp = datetime.fromisoformat(value)
            # Persist UTC only and reject non-canonical, naive, local or
            # malformed values. Never silently reset an invalid checkpoint.
            if (stamp.tzinfo is None or stamp.utcoffset() is None
                    or stamp.utcoffset().total_seconds() != 0
                    or stamp.isoformat() != value
                    or stamp.astimezone(ET).date() != day):
                raise ValueError("bad timestamp")
            timestamps.append(stamp)
        return MonitorCheckpoint(day, *timestamps)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError("MONITOR_CHECKPOINT_CORRUPT") from exc


def _encode(timestamp):
    return timestamp.astimezone(timezone.utc).isoformat() if timestamp else None


class OfflineMonitorCheckpoints:
    """Local SQLite checkpoint store with optimistic conflict detection.

    Instantiate under a trusted, access-controlled local path outside the
    repository. Atomic writes do not make future external work exactly-once.
    """

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)) or not str(path):
            raise ValueError("INVALID_MONITOR_DB_PATH")
        self._conn = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS monitor_checkpoints (
                   session_day TEXT PRIMARY KEY NOT NULL,
                   position_at TEXT, watchlist_at TEXT,
                   discovery_at TEXT, news_at TEXT
                ) WITHOUT ROWID"""
            )
        except Exception:
            self._conn.close()
            raise

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _load(self, day: date) -> MonitorCheckpoint:
        row = self._conn.execute(
            """SELECT position_at, watchlist_at, discovery_at, news_at
               FROM monitor_checkpoints WHERE session_day=?""", (_day(day),)
        ).fetchone()
        if row is None:
            return MonitorCheckpoint(day)
        return _decode(day, row)

    def load(self, day: date) -> MonitorCheckpoint:
        """Read checkpoint without inferring that a trading day is open."""
        return self._load(day)

    def record_completed(self, *, now: datetime, session: VerifiedSession,
                         expected: MonitorCheckpoint,
                         completed: frozenset[str]) -> MonitorCheckpoint:
        """Atomically acknowledge explicitly completed due-work at a timestamp.

        Concurrent or duplicate acknowledgments with an obsolete expected
        checkpoint fail with MONITOR_CHECKPOINT_CONFLICT. Reject early/late
        sessions, halted exchange, bad dates and tasks not currently due.
        This function does not run or validate actual monitoring work.
        """
        if (type(expected) is not MonitorCheckpoint
                or type(completed) is not frozenset
                or not completed or not completed <= _TASKS.keys()):
            raise ValueError("INVALID_MONITOR_ACK")
        if not isinstance(session, VerifiedSession):
            raise ValueError("UNVERIFIED_SESSION_CALENDAR")
        if expected.day != session.day:
            raise ValueError("INVALID_MONITOR_ACK")
        # Transactional CAS protects concurrent writers to the same local DB.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            actual = self._load(session.day)
            if actual != expected:
                raise ValueError("MONITOR_CHECKPOINT_CONFLICT")
            plan = plan_monitor_tick(now=now, session=session, checkpoint=actual)
            if plan.session not in ("PREMARKET", "REGULAR"):
                raise ValueError("MONITOR_SESSION_NOT_ACTIVE")
            if any(not getattr(plan, _TASKS[name][1]) for name in completed):
                raise ValueError("MONITOR_TASK_NOT_DUE")
            # Credit only tasks explicitly acknowledged by the caller.
            updated = replace(actual, **{
                _TASKS[name][0]: now.astimezone(timezone.utc)
                for name in completed
            })
            fields = tuple(_encode(getattr(updated, key)) for key in _COLUMNS)
            self._conn.execute(
                """INSERT INTO monitor_checkpoints
                   (session_day, position_at, watchlist_at, discovery_at, news_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(session_day) DO UPDATE SET
                     position_at=excluded.position_at,
                     watchlist_at=excluded.watchlist_at,
                     discovery_at=excluded.discovery_at,
                     news_at=excluded.news_at""",
                (_day(session.day), *fields),
            )
            self._conn.execute("COMMIT")
            return updated
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
