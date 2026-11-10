"""Offline forward-paper session runner: drives the full pipeline over bars.

For each supplied session day the runner feeds caller-asserted marks to the
venue, settles fills, rebuilds the account mark, reconciles, evaluates exits
then entries via the read-only decision layer, dispatches permitted plans
through the manager, and expires working orders at session end.

Marks are *asserted-executable* prices: the caller controls which price of a
daily bar was tradable and must not feed marks that were not knowable at
`mark_time` — the runner cannot detect lookahead bias in its inputs. Signals
(scanner classification, catalyst status, optional explicit stop) are
caller-supplied for the same reason: gap_scanner/catalysts require
vendor-attested timestamps this runner cannot establish.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal as D, DecimalException, Inexact, localcontext
from itertools import islice
import re
from typing import Mapping
from zoneinfo import ZoneInfo

from .broker.base import BrokerAdapter
from .broker.manager import PaperOrderManager
from .decision import (EntryCandidate, ExitQuote, FilledPosition,
                       propose_entry, propose_exit)
from .killswitch import KillSwitch
from .orders import OrderLedger
from .reconcile import reconcile
from .reservations import OfflineReservations
from .research.backtest import Bar
from .risk import INITIAL_EQUITY, PaperAccount


_US_EASTERN = ZoneInfo("America/New_York")
# Keep caller-input identifiers aligned with paper venue and offline ledger.
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


@dataclass(frozen=True)
class SessionSignal:
    scanner_classification: str           # must be "WATCHLIST" to be eligible
    catalyst_status: str                  # must be "VERIFIED" to be eligible
    stop_price: D | None = None           # explicit stop, else mark*(1-stop_fraction)


@dataclass(frozen=True)
class SessionDay:
    """One trading day: its mark timestamp, per-symbol bars, and signals."""
    day: str                              # YYYY-MM-DD
    mark_time: datetime                   # aware; when marks were executable
    bars: Mapping[str, Bar]
    signals: Mapping[str, SessionSignal]


@dataclass(frozen=True)
class DayReport:
    day: str
    equity: D
    cash: D
    positions: tuple
    open_orders: int
    actions: tuple[str, ...]
    recon_status: str
    halted: bool


@dataclass(frozen=True)
class RunReport:
    days: tuple[DayReport, ...]
    final_equity: D
    halted: bool


class PaperSessionRunner:
    """Drives propose_* -> execute_* -> advance -> sync -> expire -> report.

    The runner owns no strategy: entries only fire for signals the caller
    classified WATCHLIST/VERIFIED, and exits only via the decision layer's
    protective-stop / take-profit rules. `mark_price` selects which bar field
    is asserted executable ("open" or "close").
    """

    def __init__(self, *, book: OfflineReservations, ledger: OrderLedger,
                 adapter: BrokerAdapter, switch: KillSwitch | None = None,
                 take_profit_pct: D = D("5"), stop_fraction: D = D("0.04"),
                 fee_budget: D = D(0), mark_price: str = "close"):
        if mark_price not in ("open", "close"):
            raise ValueError("INVALID_MARK_POLICY")
        for value in (take_profit_pct, stop_fraction, fee_budget):
            if not isinstance(value, D) or not value.is_finite() or value < 0:
                raise ValueError("INVALID_RUNNER_CONFIG")
        self._mgr = PaperOrderManager(book=book, ledger=ledger,
                                      adapter=adapter, killswitch=switch)
        self._book = book
        self._ledger = ledger
        self._adapter = adapter
        self._tp = take_profit_pct
        self._stop_frac = stop_fraction
        self._fee = fee_budget
        self._mark = mark_price
        self._versions = {}
        self._last_day = None

    def _marks(self, day: SessionDay):
        return {sym: getattr(bar, self._mark)
                for sym, bar in day.bars.items()}

    def _account(self, snap, marks):
        held = {sym for sym, qty, _cost in snap.positions if qty}
        if not held.issubset(marks):
            raise ValueError("UNVERIFIED_OR_STALE_MARK")
        # Marked equity is a financial risk boundary, not a presentation
        # calculation. Ambient Decimal precision must not round a near-2%
        # loss into (or away from) the one-way daily shutdown threshold.
        try:
            with localcontext() as ctx:
                ctx.prec = 50
                ctx.traps[Inexact] = True
                marked = sum((qty * marks[sym]
                              for sym, qty, _cost in snap.positions), D(0))
                equity = snap.cash + marked
                day_pnl = equity - INITIAL_EQUITY
        except DecimalException as exc:
            raise ValueError("INEXACT_PAPER_ACCOUNT") from exc
        return PaperAccount(equity, INITIAL_EQUITY, snap.cash,
                            day_pnl, snap.session_id)

    def _stop_of(self, symbol):
        for order in sorted(self._ledger.orders(),
                            key=lambda o: o.created_at, reverse=True):
            if order.symbol == symbol and order.side == "BUY" \
                    and order.stop_price is not None and order.filled:
                return order.stop_price
        return None

    def run(self, days) -> RunReport:
        # The current book pins the 2% loss baseline to the original $100k.
        # There is no durable, independently reconciled per-session rollover.
        # Preflight the *whole batch* before processing any fills or intents,
        # so an invalid second day cannot leave a partial first-day mutation.
        # Materialize no more than two elements, even for an unbounded
        # iterable. Two inputs are enough to reject a multi-day batch.
        days = tuple(islice(days, 2))
        if not days:
            # Without a verified mark, an empty replay must not claim the
            # original $100k is the account's current equity.
            raise ValueError("MISSING_SESSION_MARK")
        if len(days) > 1:
            raise ValueError("SESSION_ROLLOVER_UNSUPPORTED")
        if isinstance(days[0], SessionDay):
            if days[0].day != self._book.snapshot().session_id:
                raise ValueError("SESSION_ROLLOVER_UNSUPPORTED")
        reports = []
        for day in days:
            if (not isinstance(day, SessionDay) or not _aware(day.mark_time)
                    or day.mark_time.astimezone(_US_EASTERN).date().isoformat() != day.day
                    or (self._last_day is not None
                        and day.day <= self._last_day)):
                raise ValueError("INVALID_SESSION_DAY")
            marks = self._marks(day)
            # Missing held-position marks cannot be replaced by cost basis.
            held = {sym for sym, qty, _cost in self._book.snapshot().positions
                    if qty}
            if not held.issubset(marks):
                raise ValueError("UNVERIFIED_OR_STALE_MARK")
            for bar in day.bars.values():
                if bar.day.isoformat() != day.day:
                    raise ValueError("INVALID_SESSION_DAY")
            # The paper venue quantizes every mark to cents, but account
            # reporting formerly valued holdings using the unrounded mark.
            # Reject fractional-cent prices *before* venue/book mutations;
            # never allow the venue's loss observation and runner equity
            # report to disagree about the same asserted mark.
            for sym, price in marks.items():
                if type(sym) is not str or not _SYMBOL.fullmatch(sym):
                    raise ValueError("INVALID_MARKS")
                if not isinstance(price, D) or not price.is_finite() or price <= 0:
                    raise ValueError("INVALID_MARKS")
                try:
                    with localcontext() as ctx:
                        ctx.prec = 50  # same maximum precision as paper venue
                        if price != price.quantize(D("0.01")):
                            raise ValueError("INEXACT_PAPER_MARK")
                except DecimalException as exc:
                    raise ValueError("INEXACT_PAPER_MARK") from exc
            # Validate caller-provided signals before advance()/sync() can
            # settle existing intents or change durable paper state. An
            # explicit invalid stop must not silently become the default via
            # "signal.stop_price or ...".
            if not isinstance(day.signals, Mapping):
                raise ValueError("INVALID_SESSION_SIGNAL")
            for sym, signal in day.signals.items():
                if (type(sym) is not str or not _SYMBOL.fullmatch(sym)
                        or not isinstance(signal, SessionSignal)
                        or type(signal.scanner_classification) is not str
                        or type(signal.catalyst_status) is not str):
                    raise ValueError("INVALID_SESSION_SIGNAL")
                if signal.stop_price is not None and (
                        type(signal.stop_price) is not D
                        or not signal.stop_price.is_finite()
                        or signal.stop_price <= 0
                        or (sym in marks and signal.stop_price >= marks[sym])):
                    raise ValueError("INVALID_SESSION_SIGNAL")
            self._last_day = day.day
            now = day.mark_time
            actions = [f"FILL:{e.broker_order_id}"
                       for e in self._mgr.advance(marks, at=now)]
            self._mgr.sync(now=now)
            snap = self._book.snapshot()
            account = self._account(snap, marks)
            recon = reconcile(self._book, self._ledger, self._adapter, at=now)
            # Exits first: they free shares and cash for same-day re-entry.
            for sym, qty, cost in snap.positions:
                if sym not in marks:
                    continue
                self._versions[sym] = self._versions.get(sym, 0) + 1
                pending = sum((max(0, order.qty - order.filled)
                               for order in self._ledger.open_orders()
                               if order.side == "SELL" and order.symbol == sym), 0)
                plan = propose_exit(
                    FilledPosition(sym, sym, D(qty), cost / qty,
                                   self._stop_of(sym) or D(0),
                                   self._versions[sym], D(pending)),
                    ExitQuote(sym, marks[sym], marks[sym], now, True),
                    as_of=now, take_profit_pct=self._tp)
                if plan.action != "PROPOSE_PAPER_SELL":
                    continue
                # The stop is the trigger, not the limit: a protective exit
                # accepts the current asserted-executable mark.
                try:
                    view = self._mgr.execute_exit(
                        plan, symbol=sym, limit_price=marks[sym],
                        recon=recon, fee_budget=self._fee, now=now)
                    actions.append(f"EXIT:{sym}:{view.status}")
                except ValueError as exc:
                    actions.append(f"EXIT_VETO:{sym}:{exc}")
            # Settle this day's exit fills BEFORE arming the loss halt —
            # liquidation proceeds must land in the book while it is open.
            actions.extend(f"FILL:{e.broker_order_id}"
                           for e in self._mgr.advance(marks, at=now))
            self._mgr.sync(now=now)
            # Exits settle first, THEN the daily mark is observed as a real
            # equity mark: risk-reducing liquidation is never blocked by the
            # one-way loss halt it may arm. New intents face the armed halt.
            snap = self._book.snapshot()
            account = self._account(snap, marks)
            self._book.observe_daily_loss(account, reconciled=True)
            # Entries: only signals the caller marked eligible.
            for sym, signal in sorted(day.signals.items()):
                if sym not in marks:
                    continue
                snap = self._book.snapshot()
                if snap.halted:
                    actions.append(f"ENTRY_VETO:{sym}:PAPER_BOOK_HALTED")
                    break
                held = next((c for s, _q, c in snap.positions if s == sym), D(0))
                pending = sum((v for i, s, _q, v in snap.pending if s == sym),
                              D(0))
                stop = signal.stop_price or marks[sym] * (D(1) - self._stop_frac)
                plan = propose_entry(
                    account,
                    EntryCandidate(sym, marks[sym], stop, now,
                                   signal.scanner_classification,
                                   signal.catalyst_status, marks[sym], sym,
                                   True, held, pending),
                    as_of=now, expected_session_id=snap.session_id,
                    reserved_cash=snap.reserved_cash, reconciled=True)
                if not plan.permitted:
                    actions.append(f"ENTRY_SKIP:{sym}:{plan.reason}")
                    continue
                try:
                    view = self._mgr.execute_entry(
                        plan, self._account(snap, marks), recon=recon,
                        fee_budget=self._fee, now=now, reconciled=True)
                    actions.append(f"ENTRY:{sym}:{view.status}")
                except ValueError as exc:
                    actions.append(f"ENTRY_VETO:{sym}:{exc}")
            # Orders dispatched today meet the same asserted-executable mark;
            # a limit at the mark fills same-day, then leftovers expire EOD.
            actions.extend(f"FILL:{e.broker_order_id}"
                           for e in self._mgr.advance(marks, at=now))
            self._mgr.sync(now=now)
            for broker_id in self._mgr.expire_session(at=now):
                actions.append(f"EXPIRED:{broker_id}")
            # Entries may fill after the first observation (with simulated
            # fees). Report and observe the *settled* account, not the
            # pre-entry account, or the day's P&L can be overstated.
            final = self._book.snapshot()
            settled_account = self._account(final, marks)
            self._book.observe_daily_loss(settled_account, reconciled=True)
            final = self._book.snapshot()
            reports.append(DayReport(
                day.day, settled_account.equity, final.cash, final.positions,
                len(self._ledger.open_orders()), tuple(actions),
                recon.status, final.halted))
        return RunReport(tuple(reports),
                         reports[-1].equity if reports else INITIAL_EQUITY,
                         bool(reports and reports[-1].halted))
