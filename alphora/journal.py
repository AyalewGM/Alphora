"""Local, offline, append-only research ledger for PAPER decision proposals.

This is not an OMS, account ledger, risk approval, broker adapter or order queue.
The hash chain detects accidental/casual record modification, not adversarial
rewrites of the entire database (there is no external trust anchor).
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re
import sqlite3

_EVENT_ID = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_SESSION = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TYPES = {"ENTRY_PROPOSAL", "EXIT_PROPOSAL", "REJECTION", "RESEARCH_NOTE"}
# Normalize punctuation and case to reject common aliases (e.g. API-Key,
# alpacaApiKey, client_secret). This is a best-effort key filter, not a DLP gate.
_SENSITIVE_KEYS = {"password", "passwd", "passphrase", "secret", "apikey",
                   "token", "authorization", "authheader", "credential",
                   "accesskey", "privatekey", "clientsecret", "accesstoken",
                   "refreshtoken", "bearertoken"}


def _sensitive_key(key: str) -> bool:
    normalized = "".join(c for c in key.casefold() if c.isascii() and c.isalnum())
    return (normalized in _SENSITIVE_KEYS or
            normalized.endswith(("apikey", "secret", "token", "password",
                                 "passphrase", "privatekey", "credential",
                                 "accesskey")) or
            normalized.startswith("authorization"))
_GENESIS = "0" * 64
_MAX_PAYLOAD_BYTES = 16_384


class JournalIntegrityError(RuntimeError):
    """Local journal content no longer matches its stored hash chain."""


@dataclass(frozen=True)
class PaperEvidence:
    event_id: str
    kind: str
    symbol: str
    session_id: str
    as_of: datetime
    payload: dict
    mode: str = "paper"


@dataclass(frozen=True)
class Receipt:
    sequence: int
    status: str  # CREATED or DUPLICATE, never permission to trade
    record_hash: str


def _no_float_or_secret(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _sensitive_key(key):
                raise ValueError("INVALID_OR_SENSITIVE_PAYLOAD")
            _no_float_or_secret(item)
    elif isinstance(value, list):
        for item in value:
            _no_float_or_secret(item)
    elif value is not None and type(value) not in (str, int, bool):
        # Financial amounts must be exact decimal *strings*, not binary floats.
        raise ValueError("INVALID_OR_SENSITIVE_PAYLOAD")


def _canonical(evidence: PaperEvidence) -> str:
    if (not isinstance(evidence, PaperEvidence) or evidence.mode != "paper" or
            not isinstance(evidence.event_id, str) or not _EVENT_ID.fullmatch(evidence.event_id) or
            not isinstance(evidence.kind, str) or evidence.kind not in _TYPES or
            not isinstance(evidence.symbol, str) or not _SYMBOL.fullmatch(evidence.symbol) or
            not isinstance(evidence.session_id, str) or not _SESSION.fullmatch(evidence.session_id) or
            not isinstance(evidence.as_of, datetime) or evidence.as_of.tzinfo is None or
            evidence.as_of.utcoffset() is None or not isinstance(evidence.payload, dict)):
        raise ValueError("INVALID_PAPER_EVIDENCE")
    try:
        if date.fromisoformat(evidence.session_id).isoformat() != evidence.session_id:
            raise ValueError("INVALID_SESSION_DATE")
    except ValueError as exc:
        raise ValueError("INVALID_PAPER_EVIDENCE") from exc
    _no_float_or_secret(evidence.payload)
    normalized = {
        "event_id": evidence.event_id, "kind": evidence.kind,
        "symbol": evidence.symbol, "session_id": evidence.session_id,
        "as_of": evidence.as_of.astimezone(timezone.utc).isoformat(),
        "payload": evidence.payload, "mode": "paper",
    }
    try:
        data = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("INVALID_PAPER_EVIDENCE") from exc
    if len(data.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("PAYLOAD_TOO_LARGE")
    return data


def _digest(previous: str, canonical: str) -> str:
    return sha256((previous + "\n" + canonical).encode("utf-8")).hexdigest()


class PaperJournal:
    """SQLite-backed, immutable research evidence; no execution methods.

    Each operation uses a fresh connection. BEGIN IMMEDIATE serializes writers
    across processes; duplicates are checked within that transaction.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if (not self.path.strip() or self.path == ":memory:" or
                self.path.startswith("file:")):
            raise ValueError("DURABLE_PATH_REQUIRED")
        connection = self._connect()
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS evidence (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                canonical TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                record_hash TEXT NOT NULL
            )""")
            connection.commit()
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _verified_rows(connection):
        rows = connection.execute(
            "SELECT sequence,event_id,canonical,previous_hash,record_hash "
            "FROM evidence ORDER BY sequence").fetchall()
        # AUTOINCREMENT preserves the highest issued sequence after ordinary
        # DELETEs. Detect accidental truncation of the tail before an empty
        # or shortened journal can silently start a fresh hash chain.
        # This is not an external trust anchor: a privileged actor can also
        # rewrite sqlite_sequence or replace the entire database.
        checkpoint = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='evidence'"
        ).fetchone()
        issued_sequence = checkpoint[0] if checkpoint else 0
        retained_sequence = rows[-1][0] if rows else 0
        if (type(issued_sequence) is not int
                or issued_sequence != retained_sequence):
            raise JournalIntegrityError("JOURNAL_INTEGRITY_FAILURE")
        previous = _GENESIS
        for expected_sequence, (sequence, event_id, canonical, prev, digest) in enumerate(rows, 1):
            try:
                obj = json.loads(canonical)
                valid = (isinstance(obj, dict) and obj.get("event_id") == event_id
                         and obj.get("mode") == "paper"
                         and json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True, allow_nan=False) == canonical)
            except (ValueError, TypeError):
                valid = False
            if (not valid or sequence != expected_sequence or prev != previous
                    or digest != _digest(previous, canonical)):
                raise JournalIntegrityError("JOURNAL_INTEGRITY_FAILURE")
            previous = digest
        return rows

    def record(self, evidence: PaperEvidence) -> Receipt:
        canonical = _canonical(evidence)  # Validate before opening transaction.
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._verified_rows(connection)
            for sequence, event_id, existing, _previous, digest in rows:
                if event_id == evidence.event_id:
                    if existing != canonical:
                        raise ValueError("INTENT_KEY_CONFLICT")
                    connection.commit()
                    return Receipt(sequence, "DUPLICATE", digest)
            previous = rows[-1][4] if rows else _GENESIS
            digest = _digest(previous, canonical)
            cursor = connection.execute(
                "INSERT INTO evidence(event_id,canonical,previous_hash,record_hash) "
                "VALUES(?,?,?,?)", (evidence.event_id, canonical, previous, digest))
            connection.commit()
            return Receipt(cursor.lastrowid, "CREATED", digest)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replay(self) -> tuple[dict, ...]:
        """Read verified historic evidence; never apply or execute decisions."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = self._verified_rows(connection)
            return tuple({"sequence": sequence, "record_hash": digest, **json.loads(canonical)}
                         for sequence, _event_id, canonical, _prev, digest in rows)
        finally:
            connection.close()
