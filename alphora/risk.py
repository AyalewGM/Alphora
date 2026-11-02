"""Pure, fail-closed pre-trade risk policy for PAPER simulations only.

This module never contacts a broker. Use Decimal for money calculations.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

INITIAL_EQUITY = Decimal("100000")
RISK_FRACTION = Decimal("0.005")
MAX_INSTRUMENT_FRACTION = Decimal("0.10")
DAILY_LOSS_FRACTION = Decimal("0.02")
REBALANCE_FREQUENCY = "daily"


@dataclass(frozen=True)
class PaperAccount:
    equity: Decimal
    session_open_equity: Decimal
    cash: Decimal
    day_pnl: Decimal
    session_id: str
    halted: bool = False


@dataclass(frozen=True)
class BuyProposal:
    symbol: str
    quantity: Decimal
    price: Decimal
    stop_price: Decimal
    existing_instrument_value: Decimal = Decimal("0")


@dataclass(frozen=True)
class Decision:
    permitted: bool
    reason: str


def _positive_finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def validate_buy(
    account: PaperAccount,
    proposal: BuyProposal,
    *,
    expected_session_id: str,
    mode: str = "paper",
) -> Decision:
    """Reject unsafe long-only purchases; must be called again just before order simulation.

    A permitted decision is NOT an order, a fill, or approval for a live brokerage.
    """
    if mode != "paper":
        return Decision(False, "REAL_TRADING_DISABLED")
    if not account.session_id or account.session_id != expected_session_id:
        return Decision(False, "SESSION_MISMATCH")
    if not all(_positive_finite(v) for v in
               (account.equity, account.session_open_equity, proposal.quantity, proposal.price)):
        return Decision(False, "INVALID_ACCOUNT_OR_ORDER")
    if (not isinstance(account.cash, Decimal) or not account.cash.is_finite()
            or account.cash < 0 or not isinstance(account.day_pnl, Decimal)
            or not account.day_pnl.is_finite()):
        return Decision(False, "INVALID_ACCOUNT_STATE")
    if (not isinstance(proposal.stop_price, Decimal) or
            not proposal.stop_price.is_finite() or proposal.stop_price <= 0 or
            proposal.stop_price >= proposal.price):
        return Decision(False, "INVALID_STOP")
    if (not isinstance(proposal.existing_instrument_value, Decimal) or
            not proposal.existing_instrument_value.is_finite()
            or proposal.existing_instrument_value < 0):
        return Decision(False, "INVALID_EXISTING_POSITION")
    if not proposal.symbol or not proposal.symbol.isascii() or not all(
        c.isupper() or c.isdigit() or c in ".-" for c in proposal.symbol
    ):
        return Decision(False, "INVALID_SYMBOL")
    # Do not let an inconsistent reported P&L bypass the daily loss halt.
    observed_day_pnl = account.equity - account.session_open_equity
    loss_limit = -(account.session_open_equity * DAILY_LOSS_FRACTION)
    if account.halted or account.day_pnl <= loss_limit or observed_day_pnl <= loss_limit:
        return Decision(False, "DAILY_LOSS_HALT")
    # Cash-only, no deposits/withdrawals in this paper snapshot.
    if (account.cash > account.equity or
            abs(observed_day_pnl - account.day_pnl) > Decimal("0.01")):
        return Decision(False, "INCONSISTENT_ACCOUNT_STATE")
    notional = proposal.price * proposal.quantity
    planned_risk = (proposal.price - proposal.stop_price) * proposal.quantity
    if planned_risk > account.equity * RISK_FRACTION:
        return Decision(False, "TRADE_RISK_EXCEEDED")
    if proposal.existing_instrument_value + notional > account.equity * MAX_INSTRUMENT_FRACTION:
        return Decision(False, "INSTRUMENT_ALLOCATION_EXCEEDED")
    if notional > account.cash:
        return Decision(False, "INSUFFICIENT_CASH")
    return Decision(True, "PAPER_BUY_ALLOWED")
