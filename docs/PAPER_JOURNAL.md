# Offline paper decision evidence journal (research stage)

`alphora.journal.PaperJournal` persists **research-only** paper decision evidence
in local SQLite, with immutable event IDs, serialized transactions, exact
Decimal-as-string payloads, restart replay, and a tamper-evident hash chain.
Use `PaperEvidence` with an explicit `mode="paper"`, an externally chosen
64-character lowercase hexadecimal idempotency ID, a timezone-aware timestamp,
and a redacted payload containing **no credentials**. `record()` returns a
receipt with `CREATED` or `DUPLICATE`, not authorization to trade. A reused ID
with a different payload raises `INTENT_KEY_CONFLICT`.

The journal requires an explicit disk-backed SQLite filesystem path. Blank paths,
SQLite `:memory:` and `file:` URI filenames are rejected, because SQLite can
silently create transient databases for those forms. The caller remains
responsible for durable storage, backup validation and file permissions.

**This is not an OMS or a funds reservation service.** It makes no decisions,
reserves no cash, simulates no fills, submits no orders, and has no network,
Alpaca or brokerage dependencies. It is intentionally independent of the
unmerged #9 entry/exit proposal engine. After #9 and relevant governance gates
pass independent review, a future separately reviewed OMS must atomically
revalidate account state and enforce 0.5% trade risk, 10% per-instrument
allocation, 2% session-loss shutdown, and cash/pending-order limits at the
reservation point. Paper-broker connectivity requires separate owner approval.

The local hash chain detects routine corruption; the SQLite AUTOINCREMENT
sequence additionally detects ordinary deletion of the last row or even every
evidence row. It **does not authenticate origin or prevent a privileged actor
from rewriting the full chain, altering sqlite_sequence, replacing the entire
database, or restoring an older valid snapshot**. These safeguards are not an
external trust anchor. Before any operational use, require separately anchored
audit receipts, backup/restore validation, permissions, reconciliation and
concurrent crash testing.

Test: `python -m unittest discover -s tests -v`.
