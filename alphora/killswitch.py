"""Operator kill switch: halt file + persistent book halt + venue cancel-all.

Three layers, all fail-closed:

1. A **halt file** at a caller-configured path. Presence means halted; it is
   checked live before every dispatch and every status poll. Absence grants
   nothing — the file is a stop, never a permission.
2. The book's own one-way session halt (`OfflineReservations.halt`), which
   blocks all new reservations and fills until the next session.
3. `kill()` additionally cancels every open venue order via the adapter.
   Scope is deliberately cancel-only: forced liquidation is itself an
   execution path and is out of scope; the venue leaves positions untouched.

Triggers are composed by the caller: a daily-loss observation, a FATAL
reconciliation, or a manual `kill(reason)`. Ledger-side state is settled
separately by the manager's sync() — the venue cancel and the durable halt
happen even if that later step never runs.
"""
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .broker.base import BrokerAdapter
from .reservations import OfflineReservations

_MAX_REASON = 256


@dataclass(frozen=True)
class KillResult:
    halted: bool
    file_engaged: bool
    cancelled: tuple[str, ...]      # broker order ids confirmed cancelled
    errors: tuple[str, ...]         # per-order cancel failures, best effort


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


class KillSwitch:
    """Halt-file + book-halt + cancel-all; paper and future adapters alike."""

    def __init__(self, *, book: OfflineReservations,
                 adapter: BrokerAdapter, halt_file):
        if (not isinstance(book, OfflineReservations)
                or not isinstance(halt_file, (str, Path))
                or not str(halt_file).strip() or "\x00" in str(halt_file)):
            raise ValueError("INVALID_KILL_SWITCH_CONFIG")
        self._file = Path(halt_file)
        if self._file.exists() and self._file.is_dir():
            raise ValueError("INVALID_KILL_SWITCH_CONFIG")
        if not self._file.parent.is_dir():
            raise ValueError("INVALID_KILL_SWITCH_CONFIG")
        self._book = book
        self._adapter = adapter

    @property
    def halt_file(self):
        return self._file

    def file_engaged(self):
        return self._file.exists()

    def is_halted(self):
        """Live check: halt file OR the book's one-way session halt."""
        return self.file_engaged() or self._book.snapshot().halted

    def assert_clear(self):
        """Raise unless both the file and the book are unhalted right now."""
        if self.file_engaged():
            raise ValueError("KILL_SWITCH_ENGAGED:HALT_FILE")
        if self._book.snapshot().halted:
            raise ValueError("KILL_SWITCH_ENGAGED:BOOK_HALTED")

    def kill(self, reason, *, now) -> KillResult:
        """Engage the halt file, halt the book, cancel all open venue orders.

        File creation preserves the FIRST recorded reason (subsequent kills
        do not overwrite it). Order cancels are best-effort: every open order
        is attempted, failures are collected, and the halt stands regardless.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_REASON:
            raise ValueError("INVALID_REASON")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        if not self._file.exists():
            self._file.write_text(json.dumps(
                {"reason": reason,
                 "at": now.astimezone().isoformat()}, sort_keys=True))
        self._book.halt()
        cancelled, errors = [], []
        for order in self._adapter.open_orders():
            try:
                ack = self._adapter.cancel(order.broker_order_id)
                if ack.status == "CANCELLED":
                    cancelled.append(order.broker_order_id)
                else:
                    errors.append(f"{order.broker_order_id}:{ack.status}")
            except Exception as exc:  # cancel-all must not stop on one failure
                errors.append(f"{order.broker_order_id}:{exc}")
        return KillResult(True, True, tuple(cancelled), tuple(errors))

    def resume(self):
        """Remove the halt file. Cannot clear the book's one-way session halt:
        a halted book stays halted until the next session by design."""
        if self._file.exists():
            self._file.unlink()
        return KillResult(self._book.snapshot().halted, False, (), ())
