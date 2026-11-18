"""Pure, fail-closed pre-trade risk policy for PAPER simulations only.

This module never contacts a broker. Use Decimal for money calculations.
"""
from dataclasses import dataclass
from decimal import Decimal, DecimalException, Inexact, localcontext
import re

INITIAL_EQUITY = Decimal("100000")
RISK_FRACTION = Decimal("0.005")
MAX_INSTRUMENT_FRACTION = Decimal("0.10")
DAILY_LOSS_FRACTION = Decimal("0.02")
REBALANCE_FREQUENCY = "daily"
# Keep the direct risk boundary aligned with the persisted offline book.
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")

# A policy fingerprint is persisted in the SQLite book. Validate the
# Decimal tuple *before* converting it to a plain string: even a positive,
# finite, tightening value like 1e-100000 could otherwise expand to an
# unbounded fingerprint. Match the inert paper-config 64-character domain.
def _bounded_policy_fraction(value: Decimal, ceiling: Decimal) -> bool:
    if (type(value) is not Decimal or not value.is_finite()
            or not Decimal("0") < value <= ceiling):
        return False
    parts = value.as_tuple()
    if (len(parts.digits) > 64 or parts.exponent < -64
            or parts.exponent > 64):
        return False
    # Tuple bounds ensure formatting costs at most 129 characters.
    # Canonicalize only trailing fractional zeros, matching PaperRiskConfig.
    literal = format(value, "f")
    canonical = literal.rstrip("0").rstrip(".") if "." in literal else literal
    return len(canonical) <= 64


# Operator-configurable PAPER limits may only tighten the owner's hard caps.
# Daily-loss fractions are *not* per-trade allowances. A tightened loss
# fraction must be pinned by the owning durable book/session before dispatch.
@dataclass(frozen=True)
class RiskPolicy:
    risk_per_trade_fraction: Decimal = RISK_FRACTION
    max_instrument_fraction: Decimal = MAX_INSTRUMENT_FRACTION
    daily_loss_fraction: Decimal = DAILY_LOSS_FRACTION

    def __post_init__(self):
        if (not _bounded_policy_fraction(self.risk_per_trade_fraction, RISK_FRACTION)
                or not _bounded_policy_fraction(self.max_instrument_fraction,
                                                MAX_INSTRUMENT_FRACTION)
                or not _bounded_policy_fraction(self.daily_loss_fraction,
                                                DAILY_LOSS_FRACTION)):
            raise ValueError("INVALID_RISK_POLICY")

    def fingerprint(self) -> str:
        # Risk-policy identity is persisted in the offline SQLite book and
        # must never depend on a caller's Decimal precision. normalize()
        # rounds to the active context and can collapse a slightly looser
        # policy into the approved policy's identity at precision=3.
        # Formatting is exact; strip only fractional trailing zeros so
        # numerically equivalent policy values retain their existing ID.
        def canonical(value: Decimal) -> str:
            literal = format(value, "f")
            return literal.rstrip("0").rstrip(".") if "." in literal else literal

        # Old book metadata uses a two-field fingerprint for the approved
        # default 2% halt. Preserve that exact identity. A tighter limit MUST
        # produce a different persisted policy ID and fail a stale-book reopen.
        base = (canonical(self.risk_per_trade_fraction) + "|"
                + canonical(self.max_instrument_fraction))
        if self.daily_loss_fraction == DAILY_LOSS_FRACTION:
            return base
        return base + "|" + canonical(self.daily_loss_fraction)


DEFAULT_RISK_POLICY = RiskPolicy()


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
    # Amount of existing_instrument_value that represents already-counted
    # pending BUYs; direct holdings-only callers leave this at zero.
    # The durable book independently reconstructs both components.
    pending_included_value: Decimal = Decimal("0")


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
    policy: RiskPolicy = DEFAULT_RISK_POLICY,
) -> Decision:
    """Reject unsafe long-only purchases; must be called again just before order simulation.

    A permitted decision is NOT an order, a fill, or approval for a live brokerage.
    """
    if mode != "paper":
        return Decision(False, "REAL_TRADING_DISABLED")
    if not isinstance(policy, RiskPolicy):
        return Decision(False, "INVALID_RISK_POLICY")
    # Invalid/untyped paper snapshots must never be interpreted as an
    # affirmative risk decision. In particular, `0`/None are not a valid
    # representation of a cleared persistent kill switch.
    if not isinstance(account, PaperAccount) or not isinstance(proposal, BuyProposal):
        return Decision(False, "INVALID_ACCOUNT_OR_ORDER")
    if type(account.halted) is not bool:
        return Decision(False, "INVALID_ACCOUNT_STATE")
    if (not isinstance(expected_session_id, str) or not expected_session_id
            or not isinstance(account.session_id, str) or not account.session_id
            or account.session_id != expected_session_id):
        return Decision(False, "SESSION_MISMATCH")
    if not all(_positive_finite(v) for v in
               (account.equity, account.session_open_equity, proposal.quantity, proposal.price)):
        return Decision(False, "INVALID_ACCOUNT_OR_ORDER")
    # Direct callers of the final risk guard must not bypass the whole-share
    # contract of the offline decision engine with fractional order proposals.
    if proposal.quantity != proposal.quantity.to_integral_value():
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
    if (not isinstance(proposal.pending_included_value, Decimal)
            or not proposal.pending_included_value.is_finite()
            or proposal.pending_included_value < 0
            or proposal.pending_included_value > proposal.existing_instrument_value):
        return Decision(False, "INVALID_PENDING_POSITION")
    if not isinstance(proposal.symbol, str) or not _SYMBOL.fullmatch(proposal.symbol):
        return Decision(False, "INVALID_SYMBOL")
    # Malformed extreme Decimal values can overflow calculations despite
    # passing the finite-input test. Return a deterministic veto, never an
    # uncaught arithmetic exception or affirmative paper decision.
    try:
        # Reject any rounded accounting calculation; a small loss or
        # allocation excess must not disappear at the Decimal precision boundary.
        with localcontext() as context:
            # Use a fixed precision for every direct caller, just as the
            # offline paper venue does. A caller's precision=3 must not
            # spuriously reject a valid cent-priced trade; still trap any
            # inexact >50-digit arithmetic rather than round away risk.
            context.prec = 50
            context.traps[Inexact] = True
            # Do not let an inconsistent reported P&L bypass the daily loss halt.
            observed_day_pnl = account.equity - account.session_open_equity
            # Use the effective, session-pinned limit, never an independent
            # global 2% threshold when an approved policy is tighter.
            loss_limit = -(account.session_open_equity * policy.daily_loss_fraction)
            if account.halted or account.day_pnl <= loss_limit or observed_day_pnl <= loss_limit:
                return Decision(False, "DAILY_LOSS_HALT")
            # Cash-only, no deposits/withdrawals in this paper snapshot.
            if (account.cash > account.equity or
                    abs(observed_day_pnl - account.day_pnl) > Decimal("0.01")):
                return Decision(False, "INCONSISTENT_ACCOUNT_STATE")
            notional = proposal.price * proposal.quantity
            planned_risk = (proposal.price - proposal.stop_price) * proposal.quantity
            # Very small Decimals may round a positive order to zero in the
            # active decimal context; never treat that as a free, safe purchase.
            if notional <= 0 or planned_risk <= 0:
                return Decision(False, "INVALID_ACCOUNT_OR_ORDER")
            if planned_risk > account.equity * policy.risk_per_trade_fraction:
                return Decision(False, "TRADE_RISK_EXCEEDED")
            if proposal.existing_instrument_value + notional > account.equity * policy.max_instrument_fraction:
                return Decision(False, "INSTRUMENT_ALLOCATION_EXCEEDED")
            if notional > account.cash:
                return Decision(False, "INSUFFICIENT_CASH")
    except DecimalException:
        return Decision(False, "INVALID_ACCOUNT_OR_ORDER")
    return Decision(True, "PAPER_BUY_ALLOWED")
