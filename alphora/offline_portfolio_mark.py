"""Pure, fail-closed local mark-to-market probe for a reconciled PAPER book.

This is a synthetic research risk input, not authenticated market data, a
kill-switch persistence service, a scheduler, a broker, or an order authority.
It values longs at *bid* as a conservative liquidation-side proxy (not a
guaranteed execution price), and never invents a price for a missing mark.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D, DecimalException, Inexact, localcontext
import re
from zoneinfo import ZoneInfo

from .risk import DEFAULT_RISK_POLICY, PaperAccount, RiskPolicy

_ET = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z", re.ASCII)
MAX_MARK_AGE_SECONDS = 5
MAX_SPREAD_PERCENT = D("1")
MAX_POSITIONS = 500
MAX_SHARES_PER_SYMBOL = 1_000_000_000


@dataclass(frozen=True)
class HeldPosition:
    symbol: str
    shares: int


@dataclass(frozen=True)
class PortfolioMarkInput:
    session_id: str
    session_open_equity: D
    cash: D
    positions: tuple[HeldPosition, ...]
    reconciled: bool
    pending_state_clear: bool
    halted: bool
    external_cash_flow: D = D("0")


@dataclass(frozen=True)
class SourceQuote:
    symbol: str
    bid: D
    ask: D
    observed_at: datetime
    source_verified: bool


@dataclass(frozen=True)
class MarkResult:
    valid: bool
    reason: str
    account: PaperAccount | None = None
    halt_required: bool = False


def _money(value, *, positive=False):
    return (type(value) is D and value.is_finite()
            and (value > 0 if positive else value >= 0))


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _symbol(value):
    return type(value) is str and _SYMBOL.fullmatch(value) is not None


def _session_id_ok(session_id, as_of):
    if type(session_id) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", session_id):
        return False
    try:
        return date.fromisoformat(session_id) == as_of.astimezone(_ET).date()
    except (ValueError, OverflowError):
        return False


def _valid_quote(quote, as_of):
    if (type(quote) is not SourceQuote or not _symbol(quote.symbol)
            or quote.source_verified is not True
            or not _money(quote.bid, positive=True)
            or not _money(quote.ask, positive=True)
            or quote.ask < quote.bid
            or not _aware(quote.observed_at)):
        return False
    try:
        age = as_of.astimezone(timezone.utc) - quote.observed_at.astimezone(timezone.utc)
        if not timedelta(0) <= age <= timedelta(seconds=MAX_MARK_AGE_SECONDS):
            return False
        # Exact cross-product comparison, NOT a rounded percentage. Trap
        # inexact arithmetic rather than let an unsafe wide spread pass.
        with localcontext() as ctx:
            ctx.traps[Inexact] = True
            ctx.prec = 50
            return (D("200") * (quote.ask - quote.bid)
                    <= MAX_SPREAD_PERCENT * (quote.ask + quote.bid))
    except (DecimalException, OverflowError, ValueError):
        return False


def mark_reconciled_paper_portfolio(
    snapshot: PortfolioMarkInput, quotes: tuple[SourceQuote, ...], *,
    as_of: datetime, mode: str = "paper", session_verified: bool = False,
    policy: RiskPolicy = DEFAULT_RISK_POLICY,
) -> MarkResult:
    """Read-only bid-mark portfolio valuation plus proposed daily halt.

    Requires caller-attested fully reconciled positions/cash, NO pending
    uncertain buys/sells or deposits/withdrawals, and independently sourced
    quote timestamps. Caller-provided session_verified is NOT authentication.
    Invalid inputs return no account, preventing a permissive entry decision.
    If a valid session-policy loss breach is observed, downstream durable OMS must
    persist its own irreversible-for-session halt and independently verify.
    """
    if mode != "paper":
        return MarkResult(False, "REAL_TRADING_DISABLED")
    if type(policy) is not RiskPolicy:
        return MarkResult(False, "INVALID_RISK_POLICY")
    if not _aware(as_of) or not _session_id_ok(
        getattr(snapshot, "session_id", None), as_of
    ):
        return MarkResult(False, "SESSION_MISMATCH")
    if session_verified is not True:
        return MarkResult(False, "UNVERIFIED_SESSION")
    if type(snapshot) is not PortfolioMarkInput:
        return MarkResult(False, "INVALID_PORTFOLIO")
    if (snapshot.reconciled is not True
            or snapshot.pending_state_clear is not True):
        return MarkResult(False, "RECONCILIATION_REQUIRED")
    if (type(snapshot.halted) is not bool
            or not _money(snapshot.cash)
            or not _money(snapshot.session_open_equity, positive=True)
            or not _money(snapshot.external_cash_flow)
            or snapshot.external_cash_flow != 0):
        return MarkResult(False, "INVALID_OR_EXTERNAL_CASH_FLOW")
    if type(snapshot.positions) is not tuple or len(snapshot.positions) > MAX_POSITIONS:
        return MarkResult(False, "INVALID_POSITION_SET")
    symbols = set()
    for position in snapshot.positions:
        if (type(position) is not HeldPosition
                or not _symbol(position.symbol)
                or type(position.shares) is not int
                or not 0 < position.shares <= MAX_SHARES_PER_SYMBOL
                or position.symbol in symbols):
            return MarkResult(False, "INVALID_POSITION_SET")
        symbols.add(position.symbol)
    if type(quotes) is not tuple or len(quotes) != len(symbols):
        return MarkResult(False, "QUOTE_SET_MISMATCH")
    by_symbol = {}
    for quote in quotes:
        if not _valid_quote(quote, as_of):
            return MarkResult(False, "UNVERIFIED_OR_STALE_QUOTE")
        if quote.symbol in by_symbol:
            return MarkResult(False, "QUOTE_SET_MISMATCH")
        by_symbol[quote.symbol] = quote
    if set(by_symbol) != symbols:
        return MarkResult(False, "QUOTE_SET_MISMATCH")

    try:
        with localcontext() as ctx:
            ctx.traps[Inexact] = True
            ctx.prec = 50
            equity = snapshot.cash
            for position in snapshot.positions:
                equity += position.shares * by_symbol[position.symbol].bid
            day_pnl = equity - snapshot.session_open_equity
            loss_limit = snapshot.session_open_equity * policy.daily_loss_fraction
            halt = snapshot.halted or day_pnl <= -loss_limit
            account = PaperAccount(
                equity=equity, session_open_equity=snapshot.session_open_equity,
                cash=snapshot.cash, day_pnl=day_pnl,
                session_id=snapshot.session_id, halted=halt,
            )
            return MarkResult(
                True, "DAILY_LOSS_HALT_REQUIRED" if halt else "MARK_VALID",
                account, halt,
            )
    except (DecimalException, OverflowError, ValueError):
        return MarkResult(False, "UNREPRESENTABLE_VALUATION")
