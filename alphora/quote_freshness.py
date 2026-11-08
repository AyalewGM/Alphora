"""Pure offline quote freshness gate for research and hypothetical paper signals.

No market feed, venue connectivity, scheduler, broker, or order calls.
All timestamps and the session label are caller-supplied and unauthenticated.
A positive result means only that a supplied quote passed validation;
it NEVER authorizes an order, simulates a fill, or proves market provenance.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D
import re

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_SOURCE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")

# Suggested review intervals, *not* evidence of an active background monitor.
# Freshness thresholds are deliberately greater than the review intervals.
_CADENCE = {
    "POSITION": (5, D("10")),
    "WATCHLIST": (15, D("30")),
    "DISCOVERY": (60, D("120")),
}
_MICROS = D("1000000")


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    last_price: D
    bid: D
    ask: D
    market_at: datetime
    received_at: datetime
    feed_label: str


@dataclass(frozen=True)
class QuoteFreshness:
    usable: bool
    reason: str
    suggested_poll_seconds: int
    quote_age_seconds: D | None = None
    last_price: D | None = None


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _seconds(delta: timedelta) -> D:
    """Convert exact timedelta microseconds without float rounding."""
    return (D(delta.days * 86400 + delta.seconds)
            + D(delta.microseconds) / _MICROS)


def _price(value):
    return type(value) is D and value.is_finite() and value > 0


def assess_quote(
    quote: QuoteObservation | None, *, expected_symbol: str,
    evaluated_at: datetime, market_session: str,
    purpose: str = "POSITION", mode: str = "paper",
) -> QuoteFreshness:
    """Assess one *caller-attested* quote; no I/O or activity is performed.

    Purpose POSITION / WATCHLIST / DISCOVERY has suggested review cadence
    5 / 15 / 60 seconds and maximum allowable quote age 10 / 30 / 120 seconds.
    Market status must be independently established as REGULAR. Extended-hours
    handling remains disabled until a reviewed market-calendar/feed contract.
    """
    cadence, max_age = _CADENCE.get(purpose, (5, D("0")))

    def reject(reason: str) -> QuoteFreshness:
        return QuoteFreshness(False, reason, cadence)

    if mode != "paper":
        return reject("REAL_TRADING_DISABLED")
    if purpose not in _CADENCE:
        return reject("INVALID_MONITOR_PURPOSE")
    if market_session != "REGULAR":
        return reject("REGULAR_SESSION_REQUIRED")
    if not _aware(evaluated_at):
        return reject("INVALID_EVALUATION_TIME")
    if quote is None:
        return reject("MISSING_QUOTE")
    if not isinstance(quote, QuoteObservation):
        return reject("INVALID_QUOTE")
    if (not isinstance(expected_symbol, str)
            or _SYMBOL.fullmatch(expected_symbol) is None
            or quote.symbol != expected_symbol):
        return reject("SYMBOL_MISMATCH")
    if (not isinstance(quote.feed_label, str)
            or _SOURCE.fullmatch(quote.feed_label) is None):
        return reject("MISSING_FEED_PROVENANCE")
    if (not _price(quote.last_price) or not _price(quote.bid)
            or not _price(quote.ask) or quote.bid > quote.ask):
        return reject("INVALID_QUOTE_PRICES")
    if not _aware(quote.market_at) or not _aware(quote.received_at):
        return reject("INVALID_QUOTE_TIMES")
    try:
        market_lag = _seconds(quote.received_at - quote.market_at)
        receive_lag = _seconds(evaluated_at - quote.received_at)
        age = _seconds(evaluated_at - quote.market_at)
    except (OverflowError, ValueError):
        return reject("INVALID_QUOTE_TIMES")
    # A delayed or future-dated feed is not equivalent to a fresh quote.
    if min(market_lag, receive_lag, age) < 0:
        return reject("TIMESTAMP_ORDER_INVALID")
    if age > max_age:
        return reject("STALE_QUOTE")
    return QuoteFreshness(True, "FRESH_CALLER_SUPPLIED_QUOTE", cadence,
                          age, quote.last_price)
