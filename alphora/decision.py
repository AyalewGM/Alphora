"""Deterministic, read-only entry sizing and exit intent proposals.

Never submits broker orders. Inputs are externally reconciled paper snapshots;
returned plans are neither approvals nor executable instructions.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal as D, ROUND_FLOOR
from hashlib import sha256
import re

from .risk import (BuyProposal, PaperAccount, RISK_FRACTION,
                   MAX_INSTRUMENT_FRACTION, validate_buy)


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
    existing_instrument_value: D = D("0")
    pending_instrument_value: D = D("0")


@dataclass(frozen=True)
class EntryPlan:
    permitted: bool
    reason: str
    proposal: BuyProposal | None = None


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
    if not isinstance(candidate.symbol, str) or not candidate.symbol:
        return EntryPlan(False, "INVALID_SYMBOL")
    if type(max_age_seconds) is not int or max_age_seconds < 0:
        return EntryPlan(False, "INVALID_FRESHNESS_POLICY")
    if not _fresh(candidate.observed_at, as_of, max_age_seconds):
        return EntryPlan(False, "STALE_OR_FUTURE_DATA")
    if candidate.scanner_classification != "WATCHLIST":
        return EntryPlan(False, "SCANNER_NOT_ELIGIBLE")
    if candidate.catalyst_status != "VERIFIED":
        return EntryPlan(False, "CATALYST_REVIEW_REQUIRED")
    if not (_positive(candidate.price) and _positive(candidate.stop_price)
            and candidate.stop_price < candidate.price):
        return EntryPlan(False, "INVALID_ENTRY_OR_STOP")
    if not all(_nonnegative(x) for x in (reserved_cash,
                                         candidate.existing_instrument_value,
                                         candidate.pending_instrument_value)):
        return EntryPlan(False, "INVALID_RESERVATIONS")
    if not isinstance(account.cash, D) or not account.cash.is_finite() or account.cash < 0:
        return EntryPlan(False, "INVALID_ACCOUNT_STATE")
    if reserved_cash > account.cash:
        return EntryPlan(False, "OVERCOMMITTED_CASH")
    # Validate session, daily loss halt, symbol and account with an ordinary
    # one-share proposal before calculating the maximum feasible size.
    reserved_instrument = (candidate.existing_instrument_value +
                           candidate.pending_instrument_value)
    available_cash = account.cash - reserved_cash
    account_available = replace(account, cash=available_cash)
    minimum = BuyProposal(candidate.symbol, D(1), candidate.price,
                          candidate.stop_price, reserved_instrument)
    precheck = validate_buy(account_available, minimum,
                            expected_session_id=expected_session_id)
    if not precheck.permitted:
        return EntryPlan(False, precheck.reason)
    by_risk = account.equity * RISK_FRACTION / (candidate.price - candidate.stop_price)
    by_instrument = (account.equity * MAX_INSTRUMENT_FRACTION - reserved_instrument) / candidate.price
    by_cash = available_cash / candidate.price
    quantity = min(by_risk, by_instrument, by_cash).to_integral_value(rounding=ROUND_FLOOR)
    if quantity < 1:
        return EntryPlan(False, "ZERO_AFFORDABLE_SHARES")
    proposal = replace(minimum, quantity=quantity)
    checked = validate_buy(account_available, proposal,
                            expected_session_id=expected_session_id)
    return EntryPlan(checked.permitted, checked.reason,
                     proposal if checked.permitted else None)


@dataclass(frozen=True)
class FilledPosition:
    position_id: str
    symbol: str
    filled_quantity: D
    average_fill_price: D
    protective_stop_price: D
    pending_exit_quantity: D = D("0")


@dataclass(frozen=True)
class ExitQuote:
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
    if not isinstance(position.position_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", position.position_id):
        return hold("INVALID_POSITION_ID")
    if (not position.symbol or not isinstance(position.symbol, str) or
            not position.symbol.isascii() or
            not all(c.isupper() or c.isdigit() or c in ".-" for c in position.symbol)):
        return hold("INVALID_SYMBOL")
    if not (_whole(position.filled_quantity) and
            _positive(position.average_fill_price) and
            _positive(position.protective_stop_price) and
            position.protective_stop_price < position.average_fill_price and
            _nonnegative(position.pending_exit_quantity) and
            position.pending_exit_quantity <= position.filled_quantity and
            position.pending_exit_quantity == position.pending_exit_quantity.to_integral_value()):
        return hold("INVALID_POSITION_STATE")
    if position.pending_exit_quantity:
        return hold("PENDING_EXIT_RECONCILIATION")
    if not (_positive(take_profit_pct) and take_profit_pct < D("1000")):
        return hold("INVALID_TARGET")
    if type(max_age_seconds) is not int or max_age_seconds < 0:
        return hold("INVALID_FRESHNESS_POLICY")
    if quote.regular_session_verified is not True:
        return hold("UNVERIFIED_REGULAR_SESSION")
    if not _fresh(quote.observed_at, as_of, max_age_seconds):
        return hold("STALE_OR_FUTURE_QUOTE")
    if not (_positive(quote.bid) and _positive(quote.ask) and quote.bid <= quote.ask):
        return hold("INVALID_QUOTE")
    if quote.bid <= position.protective_stop_price:
        reason = "PROTECTIVE_EXIT"
    elif quote.bid >= position.average_fill_price * (D(1) + take_profit_pct / 100):
        reason = "TAKE_PROFIT"
    else:
        return hold("TARGET_NOT_REACHED")
    # Stable across repeated evaluation of the same position / exit decision.
    key = sha256(f"PAPER|{position.position_id}|{position.symbol}|EXIT".encode("utf-8")).hexdigest()
    return ExitPlan("PROPOSE_PAPER_SELL", reason, position.filled_quantity, key)
