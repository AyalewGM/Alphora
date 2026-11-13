"""Synthetic-only, caller-attested point-in-time US asset-universe receipts.

No provider authentication, exchange calendar, vendor download, corporate action
accounting, broker API, or order execution. This is a validation CONTRACT, not
proof that an asset was actually available or licensed at an historical date.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,63}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_MIC = re.compile(r"[A-Z]{4}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CLASSES = frozenset({"STOCK", "ETF"})
_REASONS = frozenset({"NOT_YET_LISTED", "DELISTED", "NOT_IN_UNIVERSE", "INCOMPLETE_EVIDENCE"})


def _date(value):
    return type(value) is date


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


@dataclass(frozen=True)
class TickerPeriod:
    symbol: str
    start: date
    end_exclusive: date | None = None


@dataclass(frozen=True)
class AssetMembership:
    # Persistent asset identity must NOT be derived from a mutable ticker.
    asset_id: str
    venue_mic: str
    asset_class: str
    listing_date: date
    delisting_date: date | None
    ticker_history: tuple[TickerPeriod, ...]
    membership_available_at: datetime  # caller-attested, not vendor-verified
    included: bool
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class UniverseReceipt:
    as_of: datetime
    source: str
    revision: str
    license_claim: str
    assets: tuple[AssetMembership, ...]
    sha256: str


def inspect_universe(assets, *, as_of, source, revision, license_claim,
                     expected_sha256=None):
    """Pin an offline manifest; fail closed on temporal contradictions.

    The resulting SHA-256 detects *local input changes only*. It is not a
    signature, an authorization to use a vendor dataset, or a PIT truth claim.
    """
    if (not _aware(as_of) or not all(isinstance(s, str) and _ID.fullmatch(s)
                                    for s in (source, revision, license_claim))
            or not isinstance(assets, (tuple, list)) or not assets
            or (expected_sha256 is not None and
                (not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256)))):
        raise ValueError("INVALID_UNIVERSE_METADATA")
    day = as_of.astimezone(_ET).date()
    ids, active_symbols = set(), set()
    rows = []
    for asset in assets:
        if not isinstance(asset, AssetMembership):
            raise ValueError("INVALID_ASSET_RECORD")
        if (not isinstance(asset.asset_id, str) or not _ID.fullmatch(asset.asset_id)
                or asset.asset_id in ids
                or not isinstance(asset.venue_mic, str) or not _MIC.fullmatch(asset.venue_mic)
                or asset.asset_class not in _CLASSES
                or not _date(asset.listing_date)
                or (asset.delisting_date is not None and
                    (not _date(asset.delisting_date)
                     or asset.delisting_date < asset.listing_date))
                or not _aware(asset.membership_available_at)
                or asset.membership_available_at > as_of
                or type(asset.included) is not bool
                or (asset.included and asset.exclusion_reason is not None)
                or (not asset.included and asset.exclusion_reason not in _REASONS)
                or not isinstance(asset.ticker_history, tuple)
                or not asset.ticker_history):
            raise ValueError("INVALID_ASSET_RECORD")
        ids.add(asset.asset_id)
        previous_end = None
        active_symbol = None
        symbols = []
        for i, item in enumerate(asset.ticker_history):
            if (not isinstance(item, TickerPeriod)
                    or not isinstance(item.symbol, str) or not _SYMBOL.fullmatch(item.symbol)
                    or not _date(item.start)
                    or (item.end_exclusive is not None and
                        (not _date(item.end_exclusive) or item.end_exclusive <= item.start))
                    or (i == 0 and item.start != asset.listing_date)
                    or (i and previous_end != item.start)
                    or (i < len(asset.ticker_history) - 1 and item.end_exclusive is None)):
                raise ValueError("INVALID_TICKER_HISTORY")
            # A historical as-of snapshot must never embed a rename that
            # only becomes effective after the snapshot date. Explicitly
            # not-yet-listed exclusions are the sole future-date exception.
            if (asset.exclusion_reason != "NOT_YET_LISTED"
                    and (item.start > day or
                         (item.end_exclusive is not None and item.end_exclusive > day))):
                raise ValueError("FUTURE_TICKER_HISTORY")
            if item.start <= day and (item.end_exclusive is None or day < item.end_exclusive):
                active_symbol = item.symbol
            previous_end = item.end_exclusive
            symbols.append({"symbol": item.symbol, "start": item.start.isoformat(),
                            "end_exclusive": (item.end_exclusive.isoformat()
                                              if item.end_exclusive else None)})
        # A future-dated delisting known only from a later historical
        # dataset cannot be smuggled into an included as-of membership.
        if asset.delisting_date is not None and asset.delisting_date > day:
            raise ValueError("FUTURE_DELISTING_EVENT")
        # A delisted asset's last ticker interval must end immediately
        # after its stated last listing day. An open-ended ticker alias
        # would contradict the delisting event and distort survivorship.
        if asset.delisting_date is not None:
            if (asset.delisting_date == date.max
                    or previous_end != asset.delisting_date + timedelta(days=1)):
                raise ValueError("INVALID_DELISTED_TICKER_HISTORY")
        is_listed = (asset.listing_date <= day and
                     (asset.delisting_date is None or day <= asset.delisting_date))
        if asset.included:
            if not is_listed or active_symbol is None:
                raise ValueError("INCLUDED_ASSET_NOT_ACTIVE")
            unique_key = (asset.venue_mic, active_symbol)
            if unique_key in active_symbols:
                raise ValueError("DUPLICATE_ACTIVE_TICKER")
            active_symbols.add(unique_key)
        elif (asset.exclusion_reason == "NOT_YET_LISTED" and asset.listing_date <= day
              or asset.exclusion_reason == "DELISTED" and
                 (asset.delisting_date is None or asset.delisting_date >= day)):
            raise ValueError("INCONSISTENT_EXCLUSION_REASON")
        rows.append({
            "asset_id": asset.asset_id, "venue_mic": asset.venue_mic,
            "asset_class": asset.asset_class,
            "listing_date": asset.listing_date.isoformat(),
            "delisting_date": (asset.delisting_date.isoformat()
                               if asset.delisting_date else None),
            "ticker_history": symbols,
            "membership_available_utc": asset.membership_available_at.astimezone(
                timezone.utc).isoformat(timespec="microseconds"),
            "included": asset.included, "exclusion_reason": asset.exclusion_reason,
        })
    document = {
        "schema": 1, "kind": "caller_attested_offline_asset_universe",
        "as_of_utc": as_of.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "source": source, "revision": revision, "license_claim": license_claim,
        "assets": sorted(rows, key=lambda x: x["asset_id"]),
    }
    digest = sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("ascii")).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError("UNIVERSE_HASH_MISMATCH")
    return UniverseReceipt(as_of, source, revision, license_claim,
                           tuple(sorted(assets, key=lambda x: x.asset_id)), digest)
