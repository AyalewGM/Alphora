"""Deterministic, offline gap-up discovery with explainable manipulation-risk signals.

No broker, network, order submission, or data downloader is imported.
All inputs must be assembled from point-in-time vendor records by a future adapter.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
import re
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


@dataclass(frozen=True)
class ScannerPolicy:
    min_price: D = D("5")
    min_gap_pct: D = D("3")
    min_adv20: int = 1_000_000
    min_avg_daily_dollar_volume: D = D("20000000")
    min_premarket_volume: int = 100_000
    min_rvol: D = D("2")
    max_spread_pct: D = D("1")
    max_quote_age_seconds: int = 120
    small_float_shares: int = 10_000_000
    extreme_gap_pct: D = D("25")


@dataclass(frozen=True)
class GapCandidate:
    symbol: str
    as_of: datetime
    session: str                    # PREMARKET or OPEN, independently calendar-verified
    listing: str                    # NYSE, NASDAQ (common shares only)
    security_type: str              # COMMON
    feed: str                       # SIP, IEX, or UNKNOWN
    source: str                     # Vendor/provenance identifier, never a secret
    previous_close: D
    previous_close_at: datetime
    reference_price: D              # Latest premarket price OR regular-session opening price
    reference_price_at: datetime
    history_through: date           # Date of last completed historical session
    adv20: int                      # Prior completed regular sessions; excludes current day
    avg_daily_dollar_volume20: D
    premarket_volume: int
    regular_cumulative_volume: int
    same_time_baseline_volume: int | None
    bid: D | None
    ask: D | None
    quote_at: datetime | None
    calendar_verified: bool
    corporate_actions_verified: bool
    credible_catalyst_at: datetime | None
    free_float_shares: int | None
    promotion_signal: bool | None   # None = unknown; never infer false from no social data
    reversal_signal: bool = False
    dilution_signal: bool = False


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    classification: str            # WATCHLIST, CAUTION, EXCLUDED (not an order permission)
    gap_pct: D | None
    rvol: D | None
    spread_pct: D | None
    reasons: tuple[str, ...]
    as_of: datetime


def _positive(value):
    return isinstance(value, D) and value.is_finite() and value > 0


def _timestamp(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _whole_nonnegative(value):
    return type(value) is int and value >= 0


def _append_once(reasons, reason):
    if reason not in reasons:
        reasons.append(reason)


def screen_one(candidate: GapCandidate, policy: ScannerPolicy = ScannerPolicy()) -> ScanResult:
    """Classify observable flags, never assert that a stock is or isn't manipulated."""
    excluded, caution = [], []
    gap, rvol, spread = None, None, None

    if not isinstance(candidate.symbol, str) or not _SYMBOL.fullmatch(candidate.symbol):
        _append_once(excluded, "INVALID_SYMBOL")
    if candidate.listing not in ("NYSE", "NASDAQ") or candidate.security_type != "COMMON":
        _append_once(excluded, "UNSUPPORTED_SECURITY")
    if candidate.session not in ("PREMARKET", "OPEN"):
        _append_once(excluded, "UNSUPPORTED_SESSION")
    if not candidate.source or candidate.feed not in ("SIP", "IEX"):
        _append_once(excluded, "MISSING_MARKET_PROVENANCE")
    elif candidate.feed != "SIP":
        _append_once(caution, "NON_CONSOLIDATED_FEED")
    if not candidate.calendar_verified:
        _append_once(excluded, "UNVERIFIED_TRADING_CALENDAR")
    if not candidate.corporate_actions_verified:
        _append_once(excluded, "UNVERIFIED_PRICE_ADJUSTMENTS")

    if not (_positive(candidate.previous_close) and _positive(candidate.reference_price)):
        _append_once(excluded, "INVALID_PRICE")
    else:
        gap = (candidate.reference_price / candidate.previous_close - 1) * 100
        if candidate.reference_price < policy.min_price:
            _append_once(excluded, "PRICE_BELOW_MINIMUM")
        if gap < policy.min_gap_pct:
            _append_once(excluded, "GAP_BELOW_MINIMUM")
        if gap >= policy.extreme_gap_pct:
            _append_once(caution, "EXTREME_GAP_REVIEW")

    if (not _timestamp(candidate.as_of) or not _timestamp(candidate.reference_price_at)
            or not _timestamp(candidate.previous_close_at)):
        _append_once(excluded, "INVALID_EVENT_TIMESTAMP")
    else:
        now = candidate.as_of.astimezone(EASTERN)
        ref = candidate.reference_price_at.astimezone(EASTERN)
        prev = candidate.previous_close_at.astimezone(EASTERN)
        if not (candidate.previous_close_at < candidate.reference_price_at <= candidate.as_of
                and prev.date() < now.date() and ref.date() == now.date()):
            _append_once(excluded, "PRICE_LOOKAHEAD_OR_SESSION_MISMATCH")
        if candidate.session == "PREMARKET":
            if not (time(4, 0) <= now.time() < time(9, 30)
                    and time(4, 0) <= ref.time() < time(9, 30)):
                _append_once(excluded, "INVALID_PREMARKET_TIME")
        elif candidate.session == "OPEN":
            if not (time(9, 30) <= now.time() < time(16, 0)
                    and time(9, 30) <= ref.time() < time(9, 31)):
                _append_once(excluded, "INVALID_OPEN_PRICE_TIME")
        if type(candidate.history_through) is not date or candidate.history_through >= now.date():
            _append_once(excluded, "HISTORY_LOOKAHEAD")

    if type(candidate.adv20) is not int or candidate.adv20 < policy.min_adv20:
        _append_once(excluded, "LOW_OR_INVALID_ADV20")
    if (not _positive(candidate.avg_daily_dollar_volume20)
            or candidate.avg_daily_dollar_volume20 < policy.min_avg_daily_dollar_volume):
        _append_once(excluded, "LOW_OR_INVALID_DOLLAR_VOLUME")
    if (not _whole_nonnegative(candidate.premarket_volume)
            or not _whole_nonnegative(candidate.regular_cumulative_volume)):
        _append_once(excluded, "INVALID_SESSION_VOLUME")
    else:
        observed = (candidate.premarket_volume if candidate.session == "PREMARKET"
                    else candidate.regular_cumulative_volume)
        if candidate.session == "PREMARKET" and observed < policy.min_premarket_volume:
            _append_once(excluded, "LOW_PREMARKET_VOLUME")
        if (type(candidate.same_time_baseline_volume) is not int
                or candidate.same_time_baseline_volume <= 0):
            _append_once(caution, "MISSING_SAME_TIME_BASELINE")
        else:
            rvol = D(observed) / D(candidate.same_time_baseline_volume)
            if rvol < policy.min_rvol:
                _append_once(excluded, "LOW_SAME_TIME_RVOL")

    if not (_positive(candidate.bid) and _positive(candidate.ask)
            and candidate.bid <= candidate.ask and _timestamp(candidate.quote_at)):
        _append_once(excluded, "MISSING_OR_INVALID_QUOTE")
    else:
        if _timestamp(candidate.as_of):
            age = candidate.as_of - candidate.quote_at
            if age < timedelta(0) or age > timedelta(seconds=policy.max_quote_age_seconds):
                _append_once(excluded, "STALE_OR_FUTURE_QUOTE")
        spread = (candidate.ask - candidate.bid) * 200 / (candidate.ask + candidate.bid)
        if spread > policy.max_spread_pct:
            _append_once(excluded, "WIDE_SPREAD")

    if candidate.credible_catalyst_at is None:
        _append_once(caution, "NO_VERIFIED_CATALYST")
    elif not _timestamp(candidate.credible_catalyst_at) or (
        _timestamp(candidate.as_of) and candidate.credible_catalyst_at > candidate.as_of
    ):
        _append_once(excluded, "FUTURE_OR_INVALID_CATALYST")
    if candidate.promotion_signal is True:
        _append_once(excluded, "PROMOTIONAL_ACTIVITY_FLAG")
    elif candidate.promotion_signal is not False:
        _append_once(caution, "PROMOTION_EVIDENCE_UNKNOWN")
    if candidate.free_float_shares is None:
        _append_once(caution, "FREE_FLOAT_UNKNOWN")
    elif type(candidate.free_float_shares) is not int or candidate.free_float_shares < 0:
        _append_once(excluded, "INVALID_FREE_FLOAT")
    elif candidate.free_float_shares < policy.small_float_shares:
        _append_once(caution, "SMALL_FREE_FLOAT")
    if candidate.reversal_signal:
        _append_once(caution, "SHARP_REVERSAL_FLAG")
    if candidate.dilution_signal:
        _append_once(caution, "RECENT_DILUTION_FLAG")

    status = "EXCLUDED" if excluded else "CAUTION" if caution else "WATCHLIST"
    return ScanResult(candidate.symbol, status, gap, rvol, spread,
                      tuple(excluded + caution), candidate.as_of)


def scan(candidates: list[GapCandidate], policy: ScannerPolicy = ScannerPolicy()) -> list[ScanResult]:
    """Rank by classification, then independently supplied liquidity and comparable RVOL.

    No order/trade eligibility can be inferred from an entry in this list.
    """
    results = [(screen_one(candidate, policy), candidate) for candidate in candidates]
    priority = {"WATCHLIST": 0, "CAUTION": 1, "EXCLUDED": 2}
    results.sort(key=lambda item: (
        priority[item[0].classification],
        -item[1].avg_daily_dollar_volume20
            if _positive(item[1].avg_daily_dollar_volume20) else D(0),
        -(item[0].rvol or D(0)),
        item[0].symbol,
    ))
    return [result for result, _ in results]
