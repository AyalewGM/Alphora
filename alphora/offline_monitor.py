"""Pure, deterministic premarket/regular-session monitoring planner.

No broker, timer, network, data provider, quote downloader, orders or fills.
A caller must supply an independently verified session-calendar receipt,
current instant, source-verified quote and persisted scheduling checkpoint.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal as D, DecimalException, localcontext
import re
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")

# Target intervals, not verified provider capabilities or guaranteed fills.
POSITION_SECONDS = 5
WATCHLIST_SECONDS = 15
DISCOVERY_SECONDS = 60
NEWS_SECONDS = 300
MAX_QUOTE_AGE_SECONDS = 5
MAX_SPREAD_PCT = D("1")


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _valid_instant(value):
    if not _aware(value):
        return False
    try:
        return value.astimezone(timezone.utc).year >= 2000
    except (OverflowError, ValueError):
        return False


def _et(day, clock):
    return datetime.combine(day, clock, tzinfo=ET)


@dataclass(frozen=True)
class VerifiedSession:
    """Externally attested *claim*; this code cannot authenticate the calendar."""
    day: date
    opens_at: datetime
    closes_at: datetime
    calendar_verified: bool
    trading_halted: bool = False


@dataclass(frozen=True)
class MonitorCheckpoint:
    day: date
    position_at: datetime | None = None
    watchlist_at: datetime | None = None
    discovery_at: datetime | None = None
    news_at: datetime | None = None


@dataclass(frozen=True)
class MonitorQuote:
    symbol: str
    bid: D
    ask: D
    observed_at: datetime
    source_verified: bool


@dataclass(frozen=True)
class MonitorDecision:
    session: str  # BLOCKED, PREMARKET, REGULAR, CLOSED, HALTED
    reason: str
    positions_due: bool = False
    watchlist_due: bool = False
    discovery_due: bool = False
    news_due: bool = False
    regular_exit_evaluation_allowed: bool = False


def _valid_session(session: VerifiedSession, now: datetime) -> bool:
    if (not isinstance(session, VerifiedSession) or type(session.day) is not date
            or session.calendar_verified is not True
            or type(session.trading_halted) is not bool
            or not _valid_instant(session.opens_at)
            or not _valid_instant(session.closes_at)
            or session.day != now.astimezone(ET).date()):
        return False
    start = _et(session.day, time(9, 30))
    close = _et(session.day, time(16))
    early = _et(session.day, time(13))
    # Never infer holidays/early closes from weekdays or from stale input.
    # Require exact published wall-clock boundaries, including DST offset.
    return (session.opens_at == start and session.opens_at.utcoffset() == start.utcoffset()
            and session.closes_at in (close, early)
            and session.closes_at.utcoffset() ==
                (close.utcoffset() if session.closes_at == close else early.utcoffset()))


def _due(last: datetime | None, now: datetime, interval: int, day: date):
    if last is None:
        return True
    if (not _valid_instant(last)
            or last.astimezone(ET).date() != day
            or last.astimezone(timezone.utc) > now.astimezone(timezone.utc)):
        raise ValueError("INVALID_MONITOR_CHECKPOINT")
    return (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)
            >= timedelta(seconds=interval))


def _quote_fresh(quote: MonitorQuote, now: datetime):
    if (not isinstance(quote, MonitorQuote) or quote.source_verified is not True
            or not isinstance(quote.symbol, str)
            or _SYMBOL.fullmatch(quote.symbol) is None
            or type(quote.bid) is not D or not quote.bid.is_finite()
            or type(quote.ask) is not D or not quote.ask.is_finite()
            or quote.bid <= 0 or quote.ask < quote.bid
            or not _valid_instant(quote.observed_at)):
        return False
    age = now.astimezone(timezone.utc) - quote.observed_at.astimezone(timezone.utc)
    if not timedelta(0) <= age <= timedelta(seconds=MAX_QUOTE_AGE_SECONDS):
        return False
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            # No optimistic rounding: reject if exact spread exceeds 1%.
            spread = (quote.ask - quote.bid) * 200 / (quote.ask + quote.bid)
            return spread <= MAX_SPREAD_PCT
    except DecimalException:
        return False


def plan_monitor_tick(*, now: datetime, session: VerifiedSession | None,
                      checkpoint: MonitorCheckpoint | None,
                      held_quote: MonitorQuote | None = None) -> MonitorDecision:
    """Plan only *due work*, never invoke network calls or mutate account state.

    Premarket: monitor and discover, but prohibit executable simulations.
    Regular: allow downstream exit evaluation ONLY with a fresh, validated
    caller-supplied held-position quote. It remains a read-only permission
    to evaluate, NOT a SELL receipt or order authorization.
    """
    if not _valid_instant(now):
        return MonitorDecision("BLOCKED", "INVALID_NOW")
    if not _valid_session(session, now):
        return MonitorDecision("BLOCKED", "UNVERIFIED_SESSION_CALENDAR")
    if session.trading_halted:
        return MonitorDecision("HALTED", "EXCHANGE_HALTED")
    local_now = now.astimezone(ET)
    premarket_start = _et(session.day, time(4))
    if local_now < premarket_start or local_now >= session.closes_at:
        return MonitorDecision("CLOSED", "OUTSIDE_APPROVED_HOURS")
    current = "PREMARKET" if local_now < session.opens_at else "REGULAR"
    if (not isinstance(checkpoint, MonitorCheckpoint)
            or type(checkpoint.day) is not date
            or checkpoint.day != session.day):
        return MonitorDecision("BLOCKED", "INVALID_MONITOR_CHECKPOINT")
    try:
        due = [_due(getattr(checkpoint, field), now, seconds, session.day)
               for field, seconds in (
                   ("position_at", POSITION_SECONDS),
                   ("watchlist_at", WATCHLIST_SECONDS),
                   ("discovery_at", DISCOVERY_SECONDS),
                   ("news_at", NEWS_SECONDS))]
    except ValueError:
        return MonitorDecision("BLOCKED", "INVALID_MONITOR_CHECKPOINT")
    # Risk checks should still occur on each quote/fill arrival regardless
    # of these periodic due flags in a future orchestration layer.
    allow_exit = current == "REGULAR" and due[0] and _quote_fresh(held_quote, now)
    reason = ("REGULAR_QUOTE_VALID" if allow_exit else
              "PREMARKET_MONITOR_ONLY" if current == "PREMARKET" else
              "QUOTE_UNVERIFIED_OR_NOT_DUE")
    return MonitorDecision(current, reason, *due, allow_exit)
