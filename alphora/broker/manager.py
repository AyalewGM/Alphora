"""Orchestrates paper intents: reservation -> ledger -> risk gate -> dispatch.

This is the only component that touches all three stores (OfflineReservations
cash book, OrderLedger, BrokerAdapter venue). It never originates a decision:
entry/exit plans come from decision.py, cash is held atomically by the book,
the order lifecycle is journaled by the ledger, and `validate_buy` is re-run
immediately before every dispatch — the risk gate stays physically in the
order path.

Paper-only: construction rejects any adapter whose mode is not "paper".
Crash recovery is `sync()`: it is idempotent and safe to re-run after any
interruption within a retained venue history. A missing SENT order is not
proof of non-dispatch because the paper adapter is volatile. Ledger and book fills are recorded in two
separate databases; a crash between them leaves the ledger ahead, which the
next idempotent sync heals — divergence beyond that is reconciliation's job.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D, DecimalException, localcontext

from ..decision import EntryPlan, ExitPlan
from ..orders import OrderLedger
from ..reconcile import Reconciliation
from ..reservations import OfflineReservations
from ..risk import PaperAccount, validate_buy
from .base import BrokerAdapter, DispatchRequest

MAX_RECON_AGE_SECONDS = 120


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _money(value):
    return isinstance(value, D) and value.is_finite() and value > 0


def _cent_price(value):
    """Require exactly representable cent ticks before any durable intent.

    The venue only dispatches cent-priced limits. A subcent stop is also
    incompatible with the venue's executable marks and understates risk.
    """
    if not _money(value):
        return False
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            return value == value.quantize(D("0.01"))
    except DecimalException:
        return False


class PaperOrderManager:
    """Drives paper order intents through book, ledger and venue."""

    def __init__(self, *, book: OfflineReservations, ledger: OrderLedger,
                 adapter: BrokerAdapter, killswitch=None):
        if getattr(adapter, "mode", None) != "paper":
            raise ValueError("REAL_TRADING_DISABLED")
        if not isinstance(book, OfflineReservations) or not isinstance(ledger, OrderLedger):
            raise ValueError("INVALID_MANAGER_COMPONENTS")
        if ledger.session_id != book.snapshot().session_id:
            raise ValueError("SESSION_MISMATCH")
        self._book = book
        self._ledger = ledger
        self._adapter = adapter
        self._switch = killswitch

    def _require_clean_recon(self, recon, now, max_age_seconds):
        """Dispatch needs a fresh, non-FATAL Reconciliation — not a bare bool.

        FATAL certificates and LOST_ACK block every dispatch here. Entries and
        exits additionally require CLEAN (see execute_entry/execute_exit);
        the remedy for WARN is sync(), which requires no certificate.
        """
        if (type(max_age_seconds) is not int
                or not 0 <= max_age_seconds <= MAX_RECON_AGE_SECONDS):
            raise ValueError("INVALID_FRESHNESS_POLICY")
        if (not isinstance(recon, Reconciliation)
                or recon.session_id != self._ledger.session_id
                or not _aware(now) or not _aware(recon.checked_at)):
            raise ValueError("STALE_OR_UNCLEAN_RECONCILIATION")
        # Same-ZoneInfo comparisons ignore fold and use wall time. Reject
        # stale or future certificates using absolute instants instead.
        try:
            now_utc = now.astimezone(timezone.utc)
            checked_utc = recon.checked_at.astimezone(timezone.utc)
        except (OverflowError, ValueError) as exc:
            raise ValueError("STALE_OR_UNCLEAN_RECONCILIATION") from exc
        if (checked_utc > now_utc
                or (now_utc - checked_utc).total_seconds() > max_age_seconds):
            raise ValueError("STALE_OR_UNCLEAN_RECONCILIATION")
        if (recon.status == "FATAL" or
                any(d.kind == "LOST_ACK" for d in recon.diffs)):
            raise ValueError("STALE_OR_UNCLEAN_RECONCILIATION")

    def _assert_dispatchable(self, *, reduce_only=False):
        if self._switch is not None:
            # A manual kill is stronger than the 2% loss halt.
            if self._switch.file_engaged():
                raise ValueError("KILL_SWITCH_ENGAGED:HALT_FILE")
            if not reduce_only:
                self._switch.assert_clear()
        if self._book.snapshot().halted and not reduce_only:
            raise ValueError("PAPER_BOOK_HALTED")

    # ---- order entry -----------------------------------------------------

    def execute_entry(self, plan: EntryPlan, account: PaperAccount, *,
                      recon, fee_budget=D(0), now, reconciled=False,
                      max_recon_age_seconds=MAX_RECON_AGE_SECONDS):
        """Reserve cash, journal, risk-check and dispatch a BUY intent.

        Idempotent across crashes: the same permitted plan always resumes the
        same reservation, ledger record and venue order via the intent key.
        A locally rejected or halted plan never reaches the venue.
        """
        if (reconciled is not True or not isinstance(plan, EntryPlan)
                or plan.permitted is not True or plan.proposal is None
                or not isinstance(plan.intent_key, str)):
            raise ValueError("INVALID_OR_UNPERMITTED_PLAN")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        self._require_clean_recon(recon, now, max_recon_age_seconds)
        # A risk-adding BUY needs a CLEAN certificate. The only permitted
        # discrepancy on idempotent retry is this plan's own never-sent
        # intent/reservation, which must retain its exact intent key.
        own = {("UNDELIVERED_INTENT", plan.intent_key),
               ("UNLINKED_RESERVATION", plan.intent_key)}
        if (recon.status not in ("CLEAN", "WARN")
                or any((d.kind, d.detail) not in own for d in recon.diffs)):
            raise ValueError("STALE_OR_UNCLEAN_RECONCILIATION")
        self._assert_dispatchable()
        proposal = plan.proposal
        # Direct manager callers must satisfy the same executable-tick
        # contract as PaperSessionRunner. Otherwise a subcent entry limit
        # reserves book cash and journals an intent *before* the venue
        # rejects dispatch; a subcent protective stop understates exposure.
        if not _cent_price(proposal.price):
            raise ValueError("INEXACT_PAPER_PRICE")
        if not _cent_price(proposal.stop_price):
            raise ValueError("INEXACT_PAPER_STOP")
        if not proposal.quantity == proposal.quantity.to_integral_value():
            raise ValueError("INVALID_QUANTITY")
        snap0 = self._book.snapshot()
        if snap0.halted:
            raise ValueError("PAPER_BOOK_HALTED")
        # Atomic cash hold with the book's own risk recheck; vetoes propagate
        # before any ledger record exists. Idempotent retry is a no-op.
        self._book.reserve_buy(plan.intent_key, account, proposal,
                               fee_budget=fee_budget, reconciled=True)
        view = self._ledger.create(
            plan.intent_key, symbol=proposal.symbol, side="BUY",
            qty=int(proposal.quantity), limit_price=proposal.price,
            stop_price=proposal.stop_price, now=now)
        # TOCTOU guard: re-run the risk gate against cash net of *other*
        # reservations — this order's own hold must not count against itself
        # (on an idempotent retry it is already inside snap0's pending holds).
        other_reserved = sum((value for ident, _sym, _qty, value
                              in snap0.pending if ident != plan.intent_key), D(0))
        guard = validate_buy(replace(account, cash=snap0.cash - other_reserved),
                             proposal, expected_session_id=snap0.session_id)
        if not guard.permitted:
            self._settle_local_reject(view, f"RISK_VETO:{guard.reason}", now)
            raise ValueError(f"RISK_VETO:{guard.reason}")
        if self._book.snapshot().halted:
            self._settle_local_reject(view, "PAPER_BOOK_HALTED", now)
            raise ValueError("PAPER_BOOK_HALTED")
        return self._send(view, now=now)

    # ---- order exit --------------------------------------------------------

    def execute_exit(self, plan: ExitPlan, *, symbol, limit_price, recon,
                     fee_budget=D(0), now,
                     max_recon_age_seconds=MAX_RECON_AGE_SECONDS):
        """Dispatch a verified reduce-only synthetic SELL.

        Merged #30 is the sole position/cash ledger; its recorded synthetic
        SELL receipt is authoritative. No competing sell reservation ledger.
        A daily-loss book halt allows reduce-only exits; a manual file halt
        blocks all orders. The venue reserves shares against overcommit.
        """
        if (not isinstance(plan, ExitPlan)
                or plan.action != "PROPOSE_PAPER_SELL"
                or not isinstance(plan.intent_key, str)
                or not isinstance(symbol, str) or plan.symbol != symbol
                or not _money(limit_price)
                or not (isinstance(plan.quantity, D)
                        and plan.quantity == plan.quantity.to_integral_value()
                        and plan.quantity > 0)):
            raise ValueError("INVALID_OR_UNACTIONABLE_PLAN")
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        # A noncent exit limit would also be rejected by the paper venue
        # after the ledger has already been mutated to SENT.
        if not _cent_price(limit_price):
            raise ValueError("INEXACT_PAPER_PRICE")
        self._require_clean_recon(recon, now, max_recon_age_seconds)
        if recon.status != "CLEAN":
            raise ValueError("STALE_OR_UNCLEAN_RECONCILIATION")
        self._assert_dispatchable(reduce_only=True)
        qty = int(plan.quantity)
        snap = self._book.snapshot()
        if snap.pending:
            raise ValueError("PENDING_BUY_RECONCILIATION")
        held = next((shares for name, shares, _cost in snap.positions
                     if name == symbol), 0)
        working = sum(max(0, order.qty - order.filled)
                      for order in self._ledger.open_orders()
                      if order.side == "SELL" and order.symbol == symbol
                      and order.client_order_id != plan.intent_key)
        if qty > held - working:
            raise ValueError("INSUFFICIENT_POSITION")
        # The paper venue must attest that the held ticker was marked in the
        # latest batch within five seconds. No cost-basis or stale quote exit.
        checker = getattr(self._adapter, "require_fresh_mark", None)
        if not callable(checker):
            raise ValueError("UNVERIFIED_OR_STALE_MARK")
        checker(symbol, at=now)
        view = self._ledger.create(
            plan.intent_key, symbol=symbol, side="SELL", qty=qty,
            limit_price=limit_price, stop_price=None, now=now)
        self._assert_dispatchable(reduce_only=True)
        return self._send(view, now=now)

    # ---- shared send path --------------------------------------------------

    def _send(self, view, *, now):
        """mark_sent -> adapter.dispatch -> ack/reject, crash-safe.

        Only a CREATED order may enter dispatch. A previously SENT order
        has an uncertain outcome and must be reconciled, never resent into
        a potentially fresh, empty paper venue.
        """
        if view.status != "CREATED":
            self.sync(now=now)
            return self._ledger.get(view.client_order_id)
        view = self._ledger.mark_sent(view.client_order_id, now=now)
        # The halt file is checked immediately before venue exposure too —
        # it may have appeared between the entry checks and this dispatch.
        self._assert_dispatchable(reduce_only=(view.side == "SELL"))
        ack = self._adapter.dispatch(
            DispatchRequest(view.symbol, view.side, view.qty,
                            view.limit_price),
            client_order_id=view.client_order_id)
        if not ack.accepted:
            self._ledger.mark_rejected(view.client_order_id,
                                       ack.reason or "VENUE_REJECTED", now=now)
            self._release_reservation(view)
            return self._ledger.get(view.client_order_id)
        return self._ledger.mark_acked(view.client_order_id,
                                       ack.broker_order_id, now=now)

    def _settle_local_reject(self, view, reason, now):
        """Terminal-reject an unsent order and release its reservation."""
        if view.status == "CREATED":
            self._ledger.mark_rejected(view.client_order_id, reason, now=now)
        self._release_reservation(view)

    def _release_reservation(self, view):
        if view.side == "BUY":
            self._book.cancel_remaining(view.client_order_id)
        else:
            # Merged #30 has no independent sell hold to release.
            # Venue/ledger open-sell quantities guard share overcommit.
            return

    # ---- cancel -------------------------------------------------------------

    def cancel(self, client_order_id, *, now):
        """Cancel an order; venue cancel is synchronous in the paper adapter."""
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        view = self._ledger.get(client_order_id)
        if view is None:
            raise ValueError("UNKNOWN_ORDER")
        if view.status == "SENT":
            self.sync(now=now)
            view = self._ledger.get(client_order_id)
            if view.status == "SENT":
                raise ValueError("UNRESOLVED_DISPATCH")
        if view.status == "CREATED":
            self._ledger.request_cancel(client_order_id, now=now)
            self._release_reservation(view)
        elif view.status in ("ACKED", "PARTIALLY_FILLED"):
            self._ledger.request_cancel(client_order_id, now=now)
            self._adapter.cancel(view.broker_order_id)
            self.sync(now=now)
        return self._ledger.get(client_order_id)

    # ---- venue events / recovery ---------------------------------------------

    def advance(self, marks, *, at):
        """Feed caller-asserted marks to the venue, then settle fills."""
        events = self._adapter.advance(marks, at=at)
        self.sync(now=at)
        return events

    def expire_session(self, *, at):
        """Expire venue DAY orders at session end, then settle the fallout."""
        expired = self._adapter.expire_session(at=at)
        self.sync(now=at)
        return expired

    def sync(self, *, now):
        """Idempotently reconcile ledger orders against asserted venue truth.

        Applies fills to ledger then book and resolves SENT lost-acks only
        when a venue record exists. A missing dispatched order is ambiguous:
        no durable venue continuity exists yet. Preserve the order and hold,
        persist quarantine, and require recovery evidence. Never infer a
        cancellation from absence alone.
        """
        if not _aware(now):
            raise ValueError("INVALID_TIMESTAMP")
        actions = []
        try:
            self._recover_terminal_buy_fills()
        except ValueError as exc:
            if str(exc) != "FILL_EXCEEDS_LIMITS":
                raise
            # Do not resume general sync or dispatch after a rejected receipt.
            # Keep the original error visible, but permit independently
            # confirmed, unfilled cancellations/expiries to release their holds.
            self._book.finish_reconciliation(None, fatal=True)
            for diagnostic in self._settle_quarantined_zero_fill_terminals(now=now):
                exc.add_note(diagnostic)
            raise
        for view in self._ledger.open_orders():
            venue = (self._adapter.order_status(view.broker_order_id)
                     if view.broker_order_id else
                     self._adapter.order_by_client(view.client_order_id))
            if venue is None:
                if view.status == "CREATED":
                    continue  # reserved but never sent; caller may retry
                # Absence of a dispatched order is not proof of non-dispatch
                # or settlement. Persist the same fatal guard for SENT and
                # acknowledged/in-flight states before exposing the error.
                # Keep ledger, receipts and reservations unchanged.
                self._book.finish_reconciliation(None, fatal=True)
                if view.status == "SENT":
                    raise RuntimeError("PAPER_VENUE_RECOVERY_REQUIRED")
                raise RuntimeError("VENUE_ORDER_MISSING")
            if view.status == "SENT":
                view = self._ledger.mark_acked(view.client_order_id,
                                               venue.broker_order_id, now=now)
                actions.append(f"ACKED:{view.client_order_id}")
            for fill in self._adapter.fills(venue.broker_order_id):
                self._record_fill(view, fill, now)
                view = self._ledger.get(view.client_order_id)
            if (venue.status == "CANCELLED" and view.status
                    in ("ACKED", "PARTIALLY_FILLED", "CANCEL_PENDING")):
                # The ledger forbids ACKED->CANCELLED directly; land the
                # in-flight cancel first, then the terminal state.
                if view.status != "CANCEL_PENDING":
                    view = self._ledger.request_cancel(view.client_order_id,
                                                       now=now)
                self._ledger.mark_cancelled(view.client_order_id, now=now)
                self._release_reservation(view)
                actions.append(f"CANCELLED:{view.client_order_id}")
            elif venue.status == "FILLED" and view.status != "FILLED":
                raise RuntimeError("VENUE_LEDGER_DIVERGENCE")
            elif venue.status == "REJECTED":
                self._ledger.mark_rejected(view.client_order_id,
                                           "VENUE_REJECTED", now=now)
                self._release_reservation(view)
                actions.append(f"REJECTED:{view.client_order_id}")
            elif venue.status == "EXPIRED":
                self._ledger.mark_expired(view.client_order_id, now=now)
                self._release_reservation(view)
                actions.append(f"EXPIRED:{view.client_order_id}")
        return tuple(actions)

    def _settle_quarantined_zero_fill_terminals(self, *, now):
        """Bounded recovery only: no fill application or order dispatch.

        Also inspect ledger-terminal orders with a remaining hold, so a crash
        between terminal ledger transition and release can be retried. A
        conflicting or partially filled order is never released by this path.
        Return order-scoped book divergence diagnostics without suppressing
        unexpected errors; the caller preserves the primary receipt veto.
        """
        diagnostics = []
        pending = {ident for ident, _sym, _qty, _value
                   in self._book.snapshot().pending}
        for view in self._ledger.orders():
            if (view.side != "BUY" or view.client_order_id not in pending
                    or view.filled != 0 or not view.broker_order_id
                    or view.status not in ("ACKED", "CANCEL_PENDING",
                                           "CANCELLED", "EXPIRED")):
                continue
            venue = self._adapter.order_status(view.broker_order_id)
            if (venue is None or venue.status not in ("CANCELLED", "EXPIRED")
                    or venue.broker_order_id != view.broker_order_id
                    or venue.client_order_id != view.client_order_id
                    or venue.symbol != view.symbol or venue.side != view.side
                    or venue.qty != view.qty or venue.filled != 0
                    or venue.limit_price != view.limit_price
                    or self._ledger.fills(view.client_order_id)
                    or self._adapter.fills(view.broker_order_id)):
                continue
            if view.status in ("CANCELLED", "EXPIRED"):
                if view.status != venue.status:
                    continue
            elif venue.status == "CANCELLED":
                self._ledger.request_cancel(view.client_order_id, now=now)
                self._ledger.mark_cancelled(view.client_order_id, now=now)
            else:
                self._ledger.mark_expired(view.client_order_id, now=now)
            try:
                self._book.cancel_remaining(
                    view.client_order_id,
                    expected_unfilled_order=(view.symbol, view.qty,
                                             view.limit_price, view.stop_price))
            except ValueError as exc:
                if str(exc) != "TERMINAL_CANCEL_RESERVATION_DIVERGENCE":
                    raise
                # The failed book transaction rolled back. Retain this hold,
                # report its identity, and inspect the next independent order.
                diagnostics.append(f"{exc}:{view.client_order_id}")
        return tuple(diagnostics)

    def _recover_terminal_buy_fills(self):
        """Replay only ledger-terminal BUYs with outstanding book holds.

        A final ledger commit can precede a failed book commit. Require the
        same terminal order and complete immutable receipts from the local
        simulated venue before touching the book. Keep the existing book
        vetoes (including daily-loss halt) and receipt deduplication intact.
        This narrow recovery does not authorize dispatch or resolve SELLs.
        """
        pending = {ident for ident, _sym, _qty, _value
                   in self._book.snapshot().pending}
        if not pending:
            return
        for view in self._ledger.orders():
            if view.status != "FILLED" or view.side != "BUY":
                continue
            if view.client_order_id not in pending:
                continue
            venue = (self._adapter.order_status(view.broker_order_id)
                     if view.broker_order_id else None)
            if (venue is None or venue.status != "FILLED"
                    or venue.broker_order_id != view.broker_order_id
                    or venue.client_order_id != view.client_order_id
                    or venue.symbol != view.symbol or venue.side != view.side
                    or venue.qty != view.qty or venue.filled != view.qty
                    or venue.limit_price != view.limit_price):
                raise RuntimeError("TERMINAL_BUY_ORDER_DIVERGENCE")
            recorded = self._ledger.fills(view.client_order_id)
            asserted = self._adapter.fills(view.broker_order_id)
            by_id = {fill.fill_id: fill for fill in asserted}
            if (len(by_id) != len(asserted) or len(by_id) != len(recorded)
                    or not recorded or sum(f.qty for f in recorded) != view.qty):
                raise RuntimeError("TERMINAL_BUY_FILL_DIVERGENCE")
            for receipt in recorded:
                fill = by_id.get(receipt.fill_id)
                if (fill is None or fill.client_order_id != view.client_order_id
                        or fill.broker_order_id != view.broker_order_id
                        or fill.symbol != view.symbol or fill.side != view.side
                        or fill.qty != receipt.qty or fill.price != receipt.price
                        or fill.fee != receipt.fee
                        or not _aware(fill.occurred_at)
                        or fill.occurred_at.astimezone(timezone.utc)
                           != receipt.occurred_at.astimezone(timezone.utc)):
                    raise RuntimeError("TERMINAL_BUY_FILL_DIVERGENCE")
            # All receipts were checked before any write. Existing receipts
            # are harmless replays; an unapplied receipt consumes its hold.
            for receipt in recorded:
                try:
                    self._book.record_hypothetical_fill(
                        view.client_order_id, receipt.fill_id, receipt.qty,
                        receipt.price, fee=receipt.fee,
                        expected_order=(view.symbol, view.qty, view.limit_price,
                                        view.stop_price))
                except ValueError as exc:
                    if str(exc) == "TERMINAL_BUY_RESERVATION_DIVERGENCE":
                        # The failed book write rolled back. Latch the existing
                        # reconciliation guard so subsequent dispatch is denied.
                        self._book.finish_reconciliation(None, fatal=True)
                    raise

    def _record_fill(self, view, fill, now):
        """Record a venue fill in ledger then book; both dedupe by fill id."""
        self._ledger.record_fill(view.client_order_id, fill.fill_id, fill.qty,
                                 fill.price, fee=fill.fee,
                                 occurred_at=fill.occurred_at, now=now)
        if view.side == "BUY":
            try:
                self._book.record_hypothetical_fill(view.client_order_id,
                                                    fill.fill_id, fill.qty,
                                                    fill.price, fee=fill.fee)
            except ValueError as exc:
                if str(exc) == "FILL_EXCEEDS_LIMITS":
                    self._book.finish_reconciliation(None, fatal=True)
                raise
        else:
            self._book.record_hypothetical_sell(fill.fill_id, fill.symbol,
                                                fill.qty, fill.price,
                                                fee=fill.fee, allow_halted=True)
