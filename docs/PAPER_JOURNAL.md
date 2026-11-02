# Offline paper decision evidence journal (research stage)

`alphora.journal.PaperJournal` persists **research-only** paper decision evidence
in local SQLite, with immutable event IDs, serialized transactions, exact
Decimal-as-string payloads, restart replay, and a tamper-evident hash chain.
Use `PaperEvidence` with an explicit `mode="paper"`, an externally chosen
64-character lowercase hexadecimal idempotency ID, a timezone-aware timestamp,
and a redacted payload containing **no credentials**. `record()` returns a
receipt with `CREATED` or `DUPLICATE`, not authorization to trade. A reused ID
with a different payload raises `INTENT_KEY_CONFLICT`.

**This is not an OMS or a funds reservation service.** It makes no decisions,
reserves no cash, simulates no fills, submits no orders, and has no network,
Alpaca or brokerage dependencies. It is intentionally independent of the
unmerged #9 entry/exit proposal engine. After #9 and relevant governance gates
pass independent review, a future separately reviewed OMS must atomically
revalidate account state and enforce 0.5% trade risk, 10% per-instrument
allocation, 2% session-loss shutdown, and cash/pending-order limits at the
reservation point. Paper-broker connectivity requires separate owner approval.

The local hash chain detects routine corruption and unsophisticated database
changes. It **does not authenticate origin or prevent a privileged actor from
rewriting the full chain or deleting the tail**. Before any operational use,
require separately anchored audit receipts, backup/restore validation,
permissions, reconciliation and concurrent crash testing.

Test: `python -m unittest discover -s tests -v`.
