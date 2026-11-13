"""Deterministic, offline gap-up discovery with explainable manipulation-risk signals.

No broker, network, order submission, or data downloader is imported.
All inputs must be assembled from point-in-time vendor records by a future adapter.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D, DecimalException, ROUND_CEILING, ROUND_FLOOR, localcontext
import re
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


@dataclass(frozen=True)
class ScannerPolicy:
    # Conservative default excludes lower-priced names from WATCHLIST.
    # Research-only; this is not a broker/order eligibility certificate.
    min_price: D = D("10")
    min_gap_pct: D = D("3")
    min_adv20: int = 1_000_000
    min_avg_daily_dollar_volume: D = D("20000000")
    min_premarket_volume: int = 100_000
    min_rvol: D = D("2")
    max_spread_pct: D = D("1")
    max_quote_age_seconds: int = 120
    small_float_shares: int = 10_000_000
    extreme_gap_pct: D = D("25")

    def __post_init__(self):
        for name in ("min_price","min_gap_pct","min_avg_daily_dollar_volume",
                     "min_rvol","max_spread_pct","extreme_gap_pct"):
            value=getattr(self,name)
            if not isinstance(value,D) or not value.is_finite() or value <= 0:
                raise ValueError("INVALID_SCANNER_POLICY")
        for name in ("min_adv20","min_premarket_volume","small_float_shares"):
            value=getattr(self,name)
            if type(value) is not int or value <= 0:
                raise ValueError("INVALID_SCANNER_POLICY")
        if (type(self.max_quote_age_seconds) is not int
                or not 0 <= self.max_quote_age_seconds <= 120):
            raise ValueError("INVALID_SCANNER_POLICY")


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
    previous_session_date: date       # verified immediately preceding trading session
    previous_session_close_at: datetime  # verified exchange regular close, e.g. 13:00 half-day
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
    if (not isinstance(candidate.source, str) or not candidate.source.strip()
            or candidate.feed not in ("SIP", "IEX")):
        _append_once(excluded, "MISSING_MARKET_PROVENANCE")
    elif candidate.feed != "SIP":
        _append_once(caution, "NON_CONSOLIDATED_FEED")
    if candidate.calendar_verified is not True:
        _append_once(excluded, "UNVERIFIED_TRADING_CALENDAR")
    if candidate.corporate_actions_verified is not True:
        _append_once(excluded, "UNVERIFIED_PRICE_ADJUSTMENTS")

    if not (_positive(candidate.previous_close) and _positive(candidate.reference_price)):
        _append_once(excluded, "INVALID_PRICE")
    else:
        try:
            gap = (candidate.reference_price / candidate.previous_close - 1) * 100
            # A rounded gap can equal a threshold even when the true ratio
            # falls just below it; enforce a conservative lower bound.
            with localcontext() as ctx:
                ctx.rounding = ROUND_FLOOR
                minimum_gap_bound = (
                    candidate.reference_price / candidate.previous_close - 1
                ) * 100
        except DecimalException:
            _append_once(excluded, "INVALID_PRICE_ARITHMETIC")
        else:
            if candidate.reference_price < policy.min_price:
                _append_once(excluded, "PRICE_BELOW_MINIMUM")
            if minimum_gap_bound < policy.min_gap_pct:
                _append_once(excluded, "GAP_BELOW_MINIMUM")
            if gap >= policy.extreme_gap_pct:
                _append_once(caution, "EXTREME_GAP_REVIEW")

    if (not _timestamp(candidate.as_of) or not _timestamp(candidate.reference_price_at)
            or not _timestamp(candidate.previous_close_at)
            or not _timestamp(candidate.previous_session_close_at)
            or type(candidate.previous_session_date) is not date):
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
        # Dates come from an independently verified exchange calendar.
        # Require the previous close to equal the expected *regular-session*
        # closing timestamp; never treat an after-hours print as "close".
        expected_close = candidate.previous_session_close_at.astimezone(EASTERN)
        if (candidate.previous_session_date >= now.date()
                or expected_close.date() != candidate.previous_session_date
                or not time(13, 0) <= expected_close.time() <= time(16, 0)):
            _append_once(excluded, "UNVERIFIED_PREVIOUS_SESSION")
        if candidate.previous_close_at != candidate.previous_session_close_at:
            _append_once(excluded, "PREVIOUS_CLOSE_NOT_REGULAR_SESSION")
        if type(candidate.history_through) is not date or candidate.history_through >= now.date():
            _append_once(excluded, "HISTORY_LOOKAHEAD")
        elif candidate.history_through != candidate.previous_session_date:
            _append_once(excluded, "STALE_OR_MISALIGNED_HISTORY")

    if type(candidate.adv20) is not int or candidate.adv20 < policy.min_adv20:
        _append_once(excluded, "LOW_OR_INVALID_ADV20")
    if (not _positive(candidate.avg_daily_dollar_volume20)
            or candidate.avg_daily_dollar_volume20 < policy.min_avg_daily_dollar_volume):
        _append_once(excluded, "LOW_OR_INVALID_DOLLAR_VOLUME")
    else:
        # A finite Decimal can exceed the active arithmetic context and
        # crash batch ranking when its descending sort key is negated.
        try:
            -candidate.avg_daily_dollar_volume20
        except DecimalException:
            _append_once(excluded, "INVALID_DOLLAR_VOLUME_ARITHMETIC")
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
            try:
                rvol = D(observed) / D(candidate.same_time_baseline_volume)
                # Avoid promoting below-threshold comparable volume when the
                # ordinary precision rounds its ratio up to the cutoff.
                with localcontext() as ctx:
                    ctx.rounding = ROUND_FLOOR
                    minimum_rvol_bound = D(observed) / D(
                        candidate.same_time_baseline_volume
                    )
            except DecimalException:
                _append_once(excluded, "INVALID_RVOL_ARITHMETIC")
            else:
                if minimum_rvol_bound < policy.min_rvol:
                    _append_once(excluded, "LOW_SAME_TIME_RVOL")

    if not (_positive(candidate.bid) and _positive(candidate.ask)
            and candidate.bid <= candidate.ask and _timestamp(candidate.quote_at)):
        _append_once(excluded, "MISSING_OR_INVALID_QUOTE")
    else:
        if _timestamp(candidate.as_of):
            age = candidate.as_of - candidate.quote_at
            if age < timedelta(0) or age > timedelta(seconds=policy.max_quote_age_seconds):
                _append_once(excluded, "STALE_OR_FUTURE_QUOTE")
        try:
            spread = (candidate.ask - candidate.bid) * 200 / (candidate.ask + candidate.bid)
            # A near-limit spread can round down to the maximum. For the
            # eligibility veto use an upper bound: numerator rounded up,
            # denominator rounded down, and quotient rounded up.
            with localcontext() as ctx:
                ctx.rounding = ROUND_CEILING
                numerator_upper = (candidate.ask - candidate.bid) * 200
            with localcontext() as ctx:
                ctx.rounding = ROUND_FLOOR
                denominator_lower = candidate.ask + candidate.bid
            with localcontext() as ctx:
                ctx.rounding = ROUND_CEILING
                maximum_spread_bound = numerator_upper / denominator_lower
        except DecimalException:
            _append_once(excluded, "INVALID_SPREAD_ARITHMETIC")
        else:
            if maximum_spread_bound > policy.max_spread_pct:
                _append_once(excluded, "WIDE_SPREAD")

    if candidate.credible_catalyst_at is None:
        _append_once(caution, "NO_VERIFIED_CATALYST")
    elif not _timestamp(candidate.credible_catalyst_at) or (
        _timestamp(candidate.as_of) and candidate.credible_catalyst_at > candidate.as_of
    ):
        _append_once(excluded, "FUTURE_OR_INVALID_CATALYST")
    elif (_timestamp(candidate.previous_close_at)
          and candidate.credible_catalyst_at <= candidate.previous_close_at):
        _append_once(caution, "NO_TIMELY_CATALYST")
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
    if type(candidate.reversal_signal) is not bool:
        _append_once(caution, "REVERSAL_EVIDENCE_UNKNOWN")
    elif candidate.reversal_signal:
        _append_once(caution, "SHARP_REVERSAL_FLAG")
    if type(candidate.dilution_signal) is not bool:
        _append_once(caution, "DILUTION_EVIDENCE_UNKNOWN")
    elif candidate.dilution_signal:
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

    def descending_dollar_volume(value):
        if not _positive(value):
            return D(0)
        try:
            return -value
        except DecimalException:
            # screen_one has already excluded unrepresentable amounts.
            return D(0)

    results.sort(key=lambda item: (
        priority[item[0].classification],
        descending_dollar_volume(item[1].avg_daily_dollar_volume20),
        -(item[0].rvol or D(0)),
        str(item[0].symbol),
    ))
    return [result for result, _ in results]
