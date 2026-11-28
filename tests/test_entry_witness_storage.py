"""Bounded storage prerequisite: never relax the legacy replay veto."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from alphora.decision import entry_intent_key
from alphora.reservations import OfflineReservations
from alphora.risk import BuyProposal, PaperAccount

SESSION = "2026-11-02"
OBS = datetime(2026, 11, 2, 10, tzinfo=timezone(timedelta(hours=-5)))
PROPOSAL = BuyProposal("SPY", D("50.0"), D("50.00"), D("40"))
ACCOUNT = PaperAccount(D("100000"), D("100000"), D("100000"), D(0), SESSION)


class EntryWitnessStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "book.sqlite"

    def open_book(self):
        book = OfflineReservations(self.path, session_id=SESSION)
        self.addCleanup(book.close)
        return book

    def read(self, sql):
        with sqlite3.connect(self.path) as db:
            return db.execute(sql).fetchall()

    def test_witness_persisted_bound_to_raw_identity_and_retry(self):
        ident = entry_intent_key(SESSION, PROPOSAL, OBS)
        book = self.open_book()
        snap = book.reserve_buy(ident, ACCOUNT, PROPOSAL,
                                reconciled=True, observed_at=OBS)
        receipt = self.read("SELECT entry_receipt FROM buy_intents")[0][0]
        self.assertEqual(json.loads(receipt),
                         [OBS.isoformat(), "50.0",
                          OBS.astimezone(timezone.utc).isoformat()])
        reopened = self.open_book()
        self.assertEqual(reopened.reserve_buy(
            ident, ACCOUNT, PROPOSAL, reconciled=True, observed_at=OBS), snap)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_ENTRY_OBSERVATION"):
            reopened.reserve_buy(ident, ACCOUNT, PROPOSAL, reconciled=True)
        later = OBS + timedelta(minutes=1)
        # This prerequisite deliberately retains the conservative veto.
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_LEGACY_ENTRY_INTENT"):
            reopened.reserve_buy(entry_intent_key(SESSION, PROPOSAL, later),
                                 ACCOUNT, PROPOSAL, reconciled=True, observed_at=later)
        self.assertEqual(reopened.snapshot(), snap)
        self.assertEqual(self.read("SELECT entry_receipt FROM buy_intents"), [(receipt,)])

    def test_v1_migration_preserves_unknown_intent_and_checking_guard(self):
        book = self.open_book()
        book.reserve_buy("legacy", ACCOUNT, PROPOSAL, reconciled=True)
        book.begin_reconciliation()
        guard = self.read("SELECT * FROM reconciliation_guard")
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE buy_intents DROP COLUMN entry_receipt")
            db.execute("PRAGMA user_version=1")
        reopened = self.open_book()
        self.assertTrue(reopened.reconciliation_guarded())
        self.assertEqual(self.read("SELECT * FROM reconciliation_guard"), guard)
        self.assertEqual(self.read("PRAGMA user_version"), [(2,)])
        self.assertEqual(self.read("SELECT id,entry_receipt FROM buy_intents"),
                         [("legacy", None)])

    def test_invalid_migration_rolls_back_schema_and_version(self):
        self.open_book()
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE buy_intents DROP COLUMN entry_receipt")
            db.execute("PRAGMA user_version=1")
            db.execute("UPDATE book_meta SET value='broken' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id=SESSION)
        self.assertEqual(self.read("PRAGMA user_version"), [(1,)])
        self.assertNotIn("entry_receipt",
                         [r[1] for r in self.read("PRAGMA table_info(buy_intents)")])

    def test_tampered_persisted_witness_fails_closed(self):
        ident = entry_intent_key(SESSION, PROPOSAL, OBS)
        book = self.open_book()
        book.reserve_buy(ident, ACCOUNT, PROPOSAL, reconciled=True, observed_at=OBS)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE buy_intents SET entry_receipt=?", (json.dumps(
                [OBS.isoformat(), "50.0", (OBS + timedelta(minutes=1)).isoformat()]),))
        with self.assertRaisesRegex(ValueError, "BOOK_STATE_CORRUPT"):
            OfflineReservations(self.path, session_id=SESSION)
