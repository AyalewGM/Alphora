"""Offline durability, integrity and idempotency tests for research evidence."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alphora.journal import PaperEvidence, PaperJournal, JournalIntegrityError


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "paper-research.sqlite"
        self.journal = PaperJournal(self.path)
        self.event = PaperEvidence("a" * 64, "ENTRY_PROPOSAL", "SPY", "2026-11-02",
                                   datetime(2026, 11, 2, 15, tzinfo=timezone.utc),
                                   {"quantity": "20", "price": "500.00", "risk_review": True})

    def test_append_duplicate_and_restart(self):
        first = self.journal.record(self.event)
        second = PaperJournal(self.path).record(self.event)
        self.assertEqual(first.status, "CREATED")
        self.assertEqual(second.status, "DUPLICATE")
        self.assertEqual(first.sequence, second.sequence)
        self.assertEqual(first.record_hash, second.record_hash)
        saved = PaperJournal(self.path).replay()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["payload"]["quantity"], "20")
        self.assertEqual(saved[0]["mode"], "paper")

    def test_conflicting_replay_fails_closed(self):
        self.journal.record(self.event)
        with self.assertRaisesRegex(ValueError, "INTENT_KEY_CONFLICT"):
            self.journal.record(replace(self.event, payload={"quantity": "21"}))
        self.assertEqual(len(self.journal.replay()), 1)

    def test_distinct_events_form_verified_chain(self):
        self.journal.record(self.event)
        next_event = replace(self.event, event_id="b" * 64,
                             as_of=self.event.as_of + timedelta(seconds=1),
                             kind="EXIT_PROPOSAL")
        self.assertEqual(self.journal.record(next_event).sequence, 2)
        self.assertEqual([x["sequence"] for x in PaperJournal(self.path).replay()], [1, 2])

    def test_concurrent_duplicate_writers_serialized(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: PaperJournal(self.path).record(self.event).status,
                                     range(12)))
        self.assertEqual(statuses.count("CREATED"), 1)
        self.assertEqual(statuses.count("DUPLICATE"), 11)
        self.assertEqual(len(self.journal.replay()), 1)

    def test_tampering_prevents_replay_and_future_appends(self):
        self.journal.record(self.event)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE evidence SET canonical=? WHERE sequence=1", ('{"fake":true}',))
        with self.assertRaises(JournalIntegrityError):
            self.journal.replay()
        with self.assertRaises(JournalIntegrityError):
            self.journal.record(replace(self.event, event_id="b" * 64))

    def test_deleted_middle_row_detected(self):
        for n in range(3):
            self.journal.record(replace(self.event, event_id=str(n) * 64))
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM evidence WHERE sequence=2")
        with self.assertRaises(JournalIntegrityError):
            self.journal.replay()

    def test_deleted_final_row_is_detected_before_replay_or_append(self):
        self.journal.record(self.event)
        second = replace(self.event, event_id="b" * 64)
        self.journal.record(second)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM evidence WHERE sequence=2")
        with self.assertRaises(JournalIntegrityError):
            PaperJournal(self.path).replay()
        with self.assertRaises(JournalIntegrityError):
            PaperJournal(self.path).record(
                replace(self.event, event_id="c" * 64)
            )

    def test_deleted_entire_journal_is_not_a_valid_empty_chain(self):
        self.journal.record(self.event)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM evidence")
        with self.assertRaises(JournalIntegrityError):
            PaperJournal(self.path).replay()
        with self.assertRaises(JournalIntegrityError):
            PaperJournal(self.path).record(self.event)

    def test_fail_closed_inputs(self):
        invalid = (
            replace(self.event, mode="live"),
            replace(self.event, event_id="short"),
            replace(self.event, symbol="SPY;DROP"),
            replace(self.event, session_id="bad-session"),
            replace(self.event, session_id="2026-99-99"),
            replace(self.event, kind=[]),
            replace(self.event, as_of=datetime(2026, 11, 2)),
            replace(self.event, payload={"risk": 0.005}),
            replace(self.event, payload={"api_key": "dont-store-this"}),
            replace(self.event, payload={"nested": [{"token": "secret"}]}),
            replace(self.event, payload={"risk": float("nan")}),
            replace(self.event, payload={"long": "x" * 20000}),
        )
        for item in invalid:
            with self.subTest(event=item):
                with self.assertRaises(ValueError):
                    self.journal.record(item)
        self.assertEqual(self.journal.replay(), ())

    def test_timezone_equivalence_is_same_event(self):
        first = self.journal.record(self.event)
        zone = timezone(timedelta(hours=-5))
        equivalent = replace(self.event, as_of=self.event.as_of.astimezone(zone))
        self.assertEqual(self.journal.record(equivalent).status, "DUPLICATE")
        self.assertEqual(first.sequence, 1)

    def test_requires_disk_path(self):
        # SQLite opens empty names as temporary DBs and recognizes file: URI
        # in-memory modes even when sqlite3.connect(uri=False) is the default.
        for path in ("", "   ", ":memory:", "file::memory:?cache=shared",
                     "file:journal?mode=memory&cache=shared"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "DURABLE_PATH_REQUIRED"):
                    PaperJournal(path)


if __name__ == "__main__":
    unittest.main()
