"""Offline data-version receipts for *candidate* point-in-time daily-bar replays.

The caller supplies vendor-attested first-seen and exact-version availability
times. This module cannot verify those attestations or independently establish
exchange calendars, survivorship, splits, dividends, or licensing.
It makes no HTTP requests, downloads, broker calls, or trading decisions.
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from .backtest import Bar, validate

_NEW_YORK = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_TYPES = frozenset({"STOCK", "ETF"})
_PRICE_BASES = frozenset({"RAW_UNADJUSTED", "SPLIT_ADJUSTED", "TOTAL_RETURN_ADJUSTED"})


def _aware(value: object) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _instant(value: datetime) -> datetime:
    """Order timestamps by UTC instant, including repeated DST wall times."""
    return value.astimezone(timezone.utc)


def _end_of_regular_session(day):
    # Conservative 16:00 ET gate; vendor/exchange calendars must still validate
    # early closes, holidays and suspended symbols.
    return datetime.combine(day, time(16, 0), tzinfo=_NEW_YORK)


def _next_regular_open(day):
    # Called only for the actual next dated bar, not a guessed business day.
    return datetime.combine(day, time(9, 30), tzinfo=_NEW_YORK)


@dataclass(frozen=True)
class ObservedDailyBar:
    bar: Bar
    first_seen_at: datetime
    version_available_at: datetime


@dataclass(frozen=True)
class DataSnapshot:
    symbol: str
    asset_type: str
    source: str
    source_revision: str
    price_basis: str
    as_of: datetime
    observations: tuple[ObservedDailyBar, ...]
    sha256: str

    @property
    def bars(self) -> tuple[Bar, ...]:
        return tuple(item.bar for item in self.observations)


def inspect_daily_snapshot(
    symbol: str,
    observations: list[ObservedDailyBar] | tuple[ObservedDailyBar, ...],
    *,
    asset_type: str,
    source: str,
    source_revision: str,
    price_basis: str,
    as_of: datetime,
    expected_sha256: str | None = None,
) -> DataSnapshot:
    """Fail closed on retroactively revised OHLCV versions used for next-open replay.

    Every *exact bar version* must have been available by the next recorded
    trading bar's 09:30 ET open. This prevents a future correction entering
    a preceding close-based trading decision. It does NOT establish vendor
    attestation truth, a complete ticker universe or corporate-action cash flows.
    """
    if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
            or not isinstance(asset_type, str) or asset_type not in _ASSET_TYPES
            or not isinstance(price_basis, str) or price_basis not in _PRICE_BASES
            or not all(isinstance(s, str) and _SOURCE.fullmatch(s)
                       for s in (source, source_revision))
            or not _aware(as_of)
            or not isinstance(observations, (list, tuple))
            or (expected_sha256 is not None and
                (not isinstance(expected_sha256, str) or
                 not _SHA256.fullmatch(expected_sha256)))):
        raise ValueError("INVALID_SNAPSHOT_METADATA")

    records = tuple(observations)
    if not all(isinstance(record, ObservedDailyBar)
               and isinstance(record.bar, Bar) for record in records):
        raise ValueError("INVALID_OBSERVATIONS")
    try:
        validate(tuple(record.bar for record in records))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("INVALID_OBSERVATIONS") from exc

    for i, record in enumerate(records):
        if (not _aware(record.first_seen_at)
                or not _aware(record.version_available_at)
                or _instant(record.first_seen_at) < _instant(_end_of_regular_session(record.bar.day))
                or _instant(record.version_available_at) < _instant(record.first_seen_at)
                or _instant(record.version_available_at) > _instant(as_of)):
            raise ValueError("UNAVAILABLE_BAR_VERSION")
        if (i + 1 < len(records) and
                _instant(record.version_available_at) >=
                _instant(_next_regular_open(records[i + 1].bar.day))):
            raise ValueError("NEXT_OPEN_LOOKAHEAD")

    def decimal_string(value):
        # Decimal.normalize() rounds to the ambient precision. Two distinct
        # vendor bar versions could otherwise receive the same pinned hash.
        # Preserve every significant digit, while removing only insignificant
        # fractional trailing zeroes for equivalent Decimal representations.
        literal = format(value, "f")
        return literal.rstrip("0").rstrip(".") if "." in literal else literal

    # Canonicalization changed in v2: previously rounded distinct Decimal
    # values could collide. Prior v1 receipts must be independently re-pinned;
    # a new digest is not an authenticated source revision.
    document = {
        "schema": 2,
        "symbol": symbol,
        "asset_type": asset_type,
        "source": source,
        "source_revision": source_revision,
        "price_basis": price_basis,
        "as_of": _utc(as_of),
        "bars": [
            {
                "day": r.bar.day.isoformat(),
                "open": decimal_string(r.bar.open),
                "high": decimal_string(r.bar.high),
                "low": decimal_string(r.bar.low),
                "close": decimal_string(r.bar.close),
                "volume": r.bar.volume,
                "first_seen_at": _utc(r.first_seen_at),
                "version_available_at": _utc(r.version_available_at),
            }
            for r in records
        ],
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("ascii")
    digest = sha256(canonical).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError("SNAPSHOT_HASH_MISMATCH")
    return DataSnapshot(symbol, asset_type, source, source_revision, price_basis,
                        as_of, records, digest)
