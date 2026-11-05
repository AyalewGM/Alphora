"""Deterministic, read-only entry sizing and exit intent proposals.

Never submits broker orders. Inputs are externally reconciled paper snapshots;
returned plans are neither approvals nor executable instructions.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal as D, ROUND_FLOOR, DecimalException, Inexact, localcontext
from hashlib import sha256
import re

from .risk import (BuyProposal, PaperAccount, RISK_FRACTION,
                   MAX_INSTRUMENT_FRACTION, DAILY_LOSS_FRACTION, validate_buy)

MAX_QUOTE_AGE_SECONDS = 120


def _positive(value):
    return isinstance(value, D) and value.is_finite() and value > 0


def _nonnegative(value):
    return isinstance(value, D) and value.is_finite() and value >= 0


def _whole(value):
    return _positive(value) and value == value.to_integral_value()


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _fresh(observed_at, as_of, max_age_seconds):
    return (_aware(observed_at) and _aware(as_of) and
            observed_at <= as_of and
            (as_of - observed_at).total_seconds() <= max_age_seconds)


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    price: D
    stop_price: D
    observed_at: datetime
    scanner_classification: str
    catalyst_status: str
    ask: D  # verified executable-side price; "price" is indicative only
    quote_symbol: str
    regular_session_verified: bool
    existing_instrument_value: D = D("0")
    pending_instrument_value: D = D("0")


@dataclass(frozen=True)
class EntryPlan:
    permitted: bool
    reason: str
    proposal: BuyProposal | None = None
    intent_key: str | None = None


def propose_entry(account: PaperAccount, candidate: EntryCandidate, *,
                  as_of: datetime, expected_session_id: str,
                  reserved_cash: D, reconciled: bool,
                  max_age_seconds: int = 120) -> EntryPlan:
    """Plan long-only *whole-share* purchases from unreserved virtual cash.

    The caller must supply a consistent reconciled snapshot. Proposals do not
    reserve cash atomically; an OMS must recheck and reserve under a transaction.
    """
    if reconciled is not True:
        return EntryPlan(False, "UNRECONCILED_STATE")
    if not isinstance(account, PaperAccount) or not isinstance(candidate, EntryCandidate):
        return EntryPlan(False, "INVALID_ACCOUNT_OR_ORDER")
    if not isinstance(candidate.symbol, str) or not candidate.symbol:
        return EntryPlan(False, "INVALID_SYMBOL")
    if type(max_age_seconds) is not int or not 0 <= max_age_seconds <= MAX_QUOTE_AGE_SECONDS:
        return EntryPlan(False, "INVALID_FRESHNESS_POLICY")
    if candidate.quote_symbol != candidate.symbol:
        return EntryPlan(False, "QUOTE_SYMBOL_MISMATCH")
    if candidate.regular_session_verified is not True:
        return EntryPlan(False, "UNVERIFIED_REGULAR_SESSION")
    if not _fresh(candidate.observed_at, as_of, max_age_seconds):
        return EntryPlan(False, "STALE_OR_FUTURE_DATA")
    if candidate.scanner_classification != "WATCHLIST":
        return EntryPlan(False, "SCANNER_NOT_ELIGIBLE")
    if candidate.catalyst_status != "VERIFIED":
        return EntryPlan(False, "CATALYST_REVIEW_REQUIRED")
    if not (_positive(candidate.price) and _positive(candidate.ask) and _positive(candidate.stop_price)
            and candidate.stop_price < candidate.ask):
        return EntryPlan(False, "INVALID_ENTRY_OR_STOP")
    if not all(_nonnegative(x) for x in (reserved_cash,
                                         candidate.existing_instrument_value,
                                         candidate.pending_instrument_value)):
        return EntryPlan(False, "INVALID_RESERVATIONS")
    if not isinstance(account.cash, D) or not account.cash.is_finite() or account.cash < 0:
        return EntryPlan(False, "INVALID_ACCOUNT_STATE")
    # Check the unreserved account first, before reservations reduce cash.
    if (isinstance(account.equity, D) and account.equity.is_finite()
            and isinstance(account.session_open_equity, D) and account.session_open_equity.is_finite()
            and isinstance(account.day_pnl, D) and account.day_pnl.is_finite()):
        try:
            loss_limit = -(account.session_open_equity * DAILY_LOSS_FRACTION)
            if (account.halted or account.day_pnl <= loss_limit
                    or account.equity - account.session_open_equity <= loss_limit):
                return EntryPlan(False, "DAILY_LOSS_HALT")
            if (account.cash > account.equity or
                    abs(account.equity - account.session_open_equity - account.day_pnl) > D("0.01")):
                return EntryPlan(False, "INCONSISTENT_ACCOUNT_STATE")
        except DecimalException:
            return EntryPlan(False, "INVALID_ACCOUNT_OR_ORDER")
    if reserved_cash > account.cash:
        return EntryPlan(False, "OVERCOMMITTED_CASH")
    # Validate session, daily loss halt, symbol and account with an ordinary
    # one-share proposal before calculating the maximum feasible size.
    try:
        # Cash and existing/pending exposure are exact ledger quantities.
        # An inexact Decimal sum/subtraction could round away a reservation
        # and allow a full-cap paper proposal to overcommit the true balance.
        with localcontext() as context:
            context.traps[Inexact] = True
            reserved_instrument = (candidate.existing_instrument_value +
                                   candidate.pending_instrument_value)
            available_cash = account.cash - reserved_cash
        account_available = replace(account, cash=available_cash)
        minimum = BuyProposal(candidate.symbol, D(1), candidate.ask,
                              candidate.stop_price, reserved_instrument)
        precheck = validate_buy(account_available, minimum,
                                expected_session_id=expected_session_id)
        if not precheck.permitted:
            return EntryPlan(False, precheck.reason)
        by_risk = account.equity * RISK_FRACTION / (candidate.ask - candidate.stop_price)
        by_instrument = (account.equity * MAX_INSTRUMENT_FRACTION - reserved_instrument) / candidate.ask
        by_cash = available_cash / candidate.ask
        quantity = min(by_risk, by_instrument, by_cash).to_integral_value(rounding=ROUND_FLOOR)
    except DecimalException:
        return EntryPlan(False, "INVALID_ACCOUNT_OR_ORDER")
    if quantity < 1:
        return EntryPlan(False, "ZERO_AFFORDABLE_SHARES")
    proposal = replace(minimum, quantity=quantity)
    checked = validate_buy(account_available, proposal,
                            expected_session_id=expected_session_id)
    if not checked.permitted:
        return EntryPlan(False, checked.reason)
    key = sha256((f"PAPER|ENTRY|{expected_session_id}|{candidate.symbol}|"
                  f"{candidate.observed_at.isoformat()}|{candidate.ask}|"
                  f"{candidate.stop_price}|{quantity}").encode("utf-8")).hexdigest()
    return EntryPlan(True, checked.reason, proposal, key)


@dataclass(frozen=True)
class FilledPosition:
    position_id: str
    symbol: str
    filled_quantity: D
    average_fill_price: D
    protective_stop_price: D
    reconciliation_version: int = 0  # positive caller-supplied position revision
    pending_exit_quantity: D = D("0")


@dataclass(frozen=True)
class ExitQuote:
    symbol: str
    bid: D
    ask: D
    observed_at: datetime
    regular_session_verified: bool


@dataclass(frozen=True)
class ExitPlan:
    action: str  # HOLD or PROPOSE_PAPER_SELL
    reason: str
    quantity: D = D("0")
    intent_key: str | None = None


def propose_exit(position: FilledPosition, quote: ExitQuote, *,
                 as_of: datetime, take_profit_pct: D,
                 max_age_seconds: int = 120) -> ExitPlan:
    """Create an idempotency-keyed SELL *intent*, never a broker order.

    Uses executable-side bid as a conservative trigger reference. Actual
    fills may differ. Protective stop has priority over take-profit.
    """
    hold = lambda reason: ExitPlan("HOLD", reason)
    if not isinstance(position, FilledPosition) or not isinstance(quote, ExitQuote):
        return hold("INVALID_POSITION_OR_QUOTE")
    if not isinstance(position.position_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", position.position_id):
        return hold("INVALID_POSITION_ID")
    if (not position.symbol or not isinstance(position.symbol, str) or
            not position.symbol.isascii() or
            not all(c.isupper() or c.isdigit() or c in ".-" for c in position.symbol)):
        return hold("INVALID_SYMBOL")
    if not (_whole(position.filled_quantity) and
            _positive(position.average_fill_price) and
            _positive(position.protective_stop_price) and
            type(position.reconciliation_version) is int and
            position.reconciliation_version > 0 and
            _nonnegative(position.pending_exit_quantity) and
            position.pending_exit_quantity <= position.filled_quantity and
            position.pending_exit_quantity == position.pending_exit_quantity.to_integral_value()):
        return hold("INVALID_POSITION_STATE")
    if quote.symbol != position.symbol:
        return hold("QUOTE_SYMBOL_MISMATCH")
    if not (_positive(take_profit_pct) and take_profit_pct < D("1000")):
        return hold("INVALID_TARGET")
    if type(max_age_seconds) is not int or not 0 <= max_age_seconds <= MAX_QUOTE_AGE_SECONDS:
        return hold("INVALID_FRESHNESS_POLICY")
    if quote.regular_session_verified is not True:
        return hold("UNVERIFIED_REGULAR_SESSION")
    if not _fresh(quote.observed_at, as_of, max_age_seconds):
        return hold("STALE_OR_FUTURE_QUOTE")
    if not (_positive(quote.bid) and _positive(quote.ask) and quote.bid <= quote.ask):
        return hold("INVALID_QUOTE")
    try:
        # An inexact target threshold can round a tiny positive profit
        # percentage down to the fill price and falsely propose a sell at
        # break-even. Reject unrepresentable thresholds, never round toward
        # an earlier exit; protective stops retain priority.
        with localcontext() as context:
            context.traps[Inexact] = True
            available_quantity = position.filled_quantity - position.pending_exit_quantity
            if not available_quantity:
                return ExitPlan("BLOCKED_REQUIRES_RECONCILIATION", "PENDING_EXIT_RECONCILIATION")
            if quote.bid <= position.protective_stop_price:
                reason = "PROTECTIVE_EXIT"
            elif quote.bid >= position.average_fill_price * (D(1) + take_profit_pct / 100):
                if position.pending_exit_quantity:
                    return ExitPlan("BLOCKED_REQUIRES_RECONCILIATION", "PENDING_EXIT_RECONCILIATION")
                reason = "TAKE_PROFIT"
            else:
                if position.pending_exit_quantity:
                    return ExitPlan("BLOCKED_REQUIRES_RECONCILIATION", "PENDING_EXIT_RECONCILIATION")
                return hold("TARGET_NOT_REACHED")
    except DecimalException:
        return hold("INVALID_POSITION_OR_QUOTE")
    # Stable across repeated evaluation of the same position / exit decision.
    key = sha256((f"PAPER|EXIT|{position.position_id}|{position.symbol}|"
                  f"{position.reconciliation_version}|{reason}|{available_quantity}")
                 .encode("utf-8")).hexdigest()
    return ExitPlan("PROPOSE_PAPER_SELL", reason, available_quantity, key)
