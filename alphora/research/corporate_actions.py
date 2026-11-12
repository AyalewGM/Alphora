"""Deterministic synthetic-only corporate-action accounting for a static long position.

Research-only: no broker, network, vendor feed, fills, strategy or account mutation.
Only RAW_UNADJUSTED closing marks are permitted. The operator attests event
availability; this module cannot authenticate that claim or the exchange calendar.
"""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, DecimalException, Inexact, localcontext
from functools import wraps
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _aware(value: object) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _money(value: object, *, positive: bool = False) -> bool:
    return (type(value) is Decimal and value.is_finite()
            and (value > 0 if positive else value >= 0))


def _date(value: object) -> bool:
    return type(value) is date


@dataclass(frozen=True)
class SessionClose:
    day: date
    opens_at: datetime
    closes_at: datetime
    raw_close: Decimal | None  # None permitted ONLY for cash-settled delisting day
    close_available_at: datetime


@dataclass(frozen=True)
class Split:
    event_id: str
    asset_id: str
    effective_on: date
    announced_at: datetime
    available_at: datetime
    numerator: int  # new shares; 2 for 2:1, 1 for 1:2 reverse split
    denominator: int  # old shares


@dataclass(frozen=True)
class CashDividend:
    event_id: str
    asset_id: str
    ex_date: date
    payable_on: date
    announced_at: datetime
    available_at: datetime
    amount_per_share: Decimal  # USD per share, raw/not total-return-adjusted


@dataclass(frozen=True)
class CashDelisting:
    event_id: str
    asset_id: str
    effective_on: date
    announced_at: datetime
    available_at: datetime
    settlement_per_share: Decimal | None  # explicit caller-attested consideration


@dataclass(frozen=True)
class LedgerDay:
    day: date
    cash: Decimal
    shares: int
    held_cost_basis: Decimal
    retired_cost_basis: Decimal
    dividend_receivable: Decimal
    gross_value: Decimal  # close mark + cash + unpaid dividend receivables
    closed: bool


@dataclass(frozen=True)
class CorporateActionReplay:
    asset_id: str
    days: tuple[LedgerDay, ...]


def _exact_ledger_arithmetic(fn):
    """Fail closed on invalid, overflowing or silently rounded ledger arithmetic."""
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            # Decimal's default precision can silently drop dollars, cash or
            # dividend entitlements on extreme/corrupt inputs. Reject those
            # snapshots rather than reporting financially incorrect totals.
            with localcontext() as context:
                context.traps[Inexact] = True
                return fn(*args, **kwargs)
        except DecimalException as exc:
            raise ValueError("UNREPRESENTABLE_LEDGER_ARITHMETIC") from exc
    return guarded


@_exact_ledger_arithmetic
def replay_static_position(*, asset_id: str, sessions: tuple[SessionClose, ...],
                           actions: tuple[Split | CashDividend | CashDelisting, ...],
                           initial_cash: Decimal, initial_shares: int,
                           initial_cost_basis: Decimal, as_of: datetime,
                           price_basis: str,
                           expected_session_days: tuple[date, ...] | None = None) -> CorporateActionReplay:
    """Apply known actions at session open, and dividends on ex-/payable dates.

    Shares are static except for splits/settlement; no trades or purchases are
    modeled. On ex-date, receivables accrue on the then-held quantity and are
    recognized in equity; they become cash on payable date. Splits change
    quantities, not aggregate basis or cash; odd-lot cash-in-lieu is unsupported.
    Cash delisting requires EXPLICIT terminal cash consideration (including
    explicitly zero), never inferred from a missing market price.
    """
    if (not isinstance(asset_id, str) or not asset_id.strip()
            or not _aware(as_of) or price_basis != "RAW_UNADJUSTED"
            or type(initial_shares) is not int or initial_shares < 0
            or not _money(initial_cash) or not _money(initial_cost_basis)
            or (initial_shares == 0 and initial_cost_basis != 0)
            or not isinstance(sessions, tuple) or not sessions
            or not isinstance(actions, tuple)):
        raise ValueError("INVALID_REPLAY_INPUT")

    by_date: dict[date, SessionClose] = {}
    previous = None
    for s in sessions:
        if (not isinstance(s, SessionClose) or not _date(s.day)
                # US equity/ETF regular closes cannot occur on weekends.
                # This is only a sanity gate, not a verified holiday calendar.
                or s.day.weekday() >= 5
                or not all(_aware(t) for t in
                           (s.opens_at, s.closes_at, s.close_available_at))
                or not s.opens_at < s.closes_at <= s.close_available_at <= as_of
                or s.opens_at.astimezone(_ET).date() != s.day
                or s.closes_at.astimezone(_ET).date() != s.day
                or s.opens_at.astimezone(_ET).time() != time(9, 30)
                or s.closes_at.astimezone(_ET).time() not in (time(13), time(16))
                or (previous is not None and
                    (s.day <= previous.day or previous.close_available_at >= s.opens_at))):
            raise ValueError("INVALID_OR_LATE_SESSION_MARK")
        if s.raw_close is not None and not _money(s.raw_close, positive=True):
            raise ValueError("INVALID_RAW_MARK")
        by_date[s.day] = s
        previous = s

    # Optional, externally supplied calendar declaration. A missing session
    # without a scheduled action is otherwise invisible to this static ledger.
    # Equality here proves only consistency with the supplied dates, never
    # calendar authenticity or market-data completeness.
    if expected_session_days is not None:
        if (not isinstance(expected_session_days, tuple)
                or not expected_session_days
                or any(not _date(d) for d in expected_session_days)
                or any(later <= earlier for earlier, later in
                       zip(expected_session_days, expected_session_days[1:]))):
            raise ValueError("INVALID_EXPECTED_SESSION_DATES")
        if tuple(by_date) != expected_session_days:
            raise ValueError("SESSION_CALENDAR_MISMATCH")

    effective: dict[date, Split | CashDividend | CashDelisting] = {}
    payments: dict[date, list[CashDividend]] = {}
    event_ids: set[str] = set()
    for e in actions:
        if not isinstance(e, (Split, CashDividend, CashDelisting)):
            raise ValueError("UNKNOWN_CORPORATE_ACTION")
        day = e.ex_date if isinstance(e, CashDividend) else e.effective_on
        if (not isinstance(e.event_id, str) or not e.event_id.strip()
                or e.event_id in event_ids or e.asset_id != asset_id
                or not _date(day) or day not in by_date or day in effective
                or not _aware(e.announced_at) or not _aware(e.available_at)
                or not e.announced_at <= e.available_at < by_date[day].opens_at):
            raise ValueError("EVENT_CONFLICT_OR_LOOKAHEAD")
        if isinstance(e, Split):
            if (type(e.numerator) is not int or type(e.denominator) is not int
                    or e.numerator <= 0 or e.denominator <= 0
                    or e.numerator == e.denominator):
                raise ValueError("INVALID_SPLIT_RATIO")
        elif isinstance(e, CashDividend):
            if (not _date(e.payable_on) or e.payable_on < day
                    or e.payable_on not in by_date
                    or not _money(e.amount_per_share, positive=True)):
                raise ValueError("INVALID_DIVIDEND")
            payments.setdefault(e.payable_on, []).append(e)
        elif not _money(e.settlement_per_share):
            # Unknown terminal valuation is not equivalent to zero recovery.
            raise ValueError("UNKNOWN_DELISTING_CONSIDERATION")
        effective[day] = e
        event_ids.add(e.event_id)

    cash, shares, held_basis = initial_cash, initial_shares, initial_cost_basis
    retired_basis = Decimal(0)
    receivable = Decimal(0)
    entitled: dict[str, Decimal] = {}
    closed = False
    days: list[LedgerDay] = []
    for index, session in enumerate(sessions):
        if closed:
            raise ValueError("SESSIONS_AFTER_DELISTING")
        day = session.day
        action = effective.get(day)
        if isinstance(action, Split):
            new_shares = shares * action.numerator
            if new_shares % action.denominator:
                raise ValueError("CASH_IN_LIEU_NOT_MODELED")
            shares = new_shares // action.denominator
        elif isinstance(action, CashDividend):
            amount = shares * action.amount_per_share
            receivable += amount
            entitled[action.event_id] = amount
        elif isinstance(action, CashDelisting):
            cash += shares * action.settlement_per_share
            retired_basis += held_basis
            held_basis = Decimal(0)
            shares = 0
            closed = True
            if index != len(sessions) - 1:
                raise ValueError("SESSIONS_AFTER_DELISTING")

        for dividend in payments.get(day, ()):
            paid = entitled.pop(dividend.event_id, None)
            if paid is None:
                raise ValueError("DIVIDEND_ENTITLEMENT_MISSING")
            receivable -= paid
            cash += paid

        if session.raw_close is None:
            if not closed:
                raise ValueError("MISSING_RAW_MARK")
            market_value = Decimal(0)
        elif closed:
            # Close after cash settlement is not a tradable holding mark.
            raise ValueError("MARK_AFTER_DELISTING")
        else:
            market_value = shares * session.raw_close

        days.append(LedgerDay(day, cash, shares, held_basis, retired_basis,
                              receivable, cash + market_value + receivable, closed))
    if entitled or receivable != 0:
        raise ValueError("UNSETTLED_DIVIDEND_OUTSIDE_WINDOW")
    return CorporateActionReplay(asset_id, tuple(days))
