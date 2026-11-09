"""Bounded, vendor-neutral *caller-attested* offline market data manifest.

This records provenance and session/universe *claims*, not verified market truth.
No market or broker connectivity, data ingestion, trading, or account mutation.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,14}\Z")
# Risk/scanner/paper ledger accept at most ten characters. Preserve
# longer historical security-master identifiers; never truncate for paper use.
_PAPER_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _valid_sha(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _aware(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _date(value):
    return type(value) is date


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SymbolPeriod:
    symbol: str
    start: date
    end: date | None = None  # inclusive; None means still applicable


@dataclass(frozen=True)
class Instrument:
    asset_id: str  # stable identifier, not the ticker
    kind: str      # STOCK or ETF
    venue: str
    listed_on: date
    delisted_on: date | None
    symbols: tuple[SymbolPeriod, ...]


@dataclass(frozen=True)
class UniverseMembership:
    day: date
    included: bool
    exclusion_reason: str | None
    available_at: datetime  # vendor asserted, not independently verified


@dataclass(frozen=True)
class ExchangeSession:
    day: date
    opens_at: datetime
    closes_at: datetime
    kind: str  # REGULAR or EARLY_CLOSE; halts are unsupported in this slice


@dataclass(frozen=True)
class MarketManifest:
    schema_version: int
    instrument: Instrument
    membership: UniverseMembership
    source_id: str
    feed: str
    revision: str
    license_grant_id: str
    authorized_for_local_research: bool
    delivered_at: datetime
    version_available_at: datetime
    artifact_sha256: str
    calendar_source: str
    calendar_version: str
    calendar_sha256: str
    calendar_available_at: datetime
    sessions: tuple[ExchangeSession, ...]
    bar_days: tuple[date, ...]
    as_of: datetime


@dataclass(frozen=True)
class ManifestReceipt:
    manifest: MarketManifest
    sha256: str  # local content fingerprint; NOT provider authentication
    paper_symbol: str | None = None  # representability only, never permission
    paper_symbol_reason: str = "NOT_ASSESSED"


def inspect_market_manifest(manifest: MarketManifest, *,
                            expected_artifact_sha256: str,
                            expected_calendar_sha256: str,
                            expected_manifest_sha256: str | None = None) -> ManifestReceipt:
    """Reject ambiguous/unlicensed or incomplete *declared* sessions and revisions.

    Expected hashes must be supplied separately by the offline data operator.
    This does not read artifact bytes or establish authentic delivery times.
    """
    if not isinstance(manifest, MarketManifest) or type(manifest.schema_version) is not int or manifest.schema_version != 1:
        raise ValueError("MANIFEST_SCHEMA")
    m = manifest
    if (not all(_valid_id(v) for v in (m.source_id, m.feed, m.revision,
                                       m.license_grant_id, m.calendar_source,
                                       m.calendar_version))
            or m.authorized_for_local_research is not True):
        raise ValueError("LICENSE_OR_SOURCE_UNVERIFIED")
    if (not all(_valid_sha(v) for v in (m.artifact_sha256, m.calendar_sha256,
                                       expected_artifact_sha256, expected_calendar_sha256))
            or (expected_manifest_sha256 is not None and not _valid_sha(expected_manifest_sha256))):
        raise ValueError("MANIFEST_HASH_FORMAT")
    if (m.artifact_sha256 != expected_artifact_sha256 or
            m.calendar_sha256 != expected_calendar_sha256):
        raise ValueError("EXTERNAL_PIN_MISMATCH")
    if (not all(_aware(t) for t in (m.delivered_at, m.version_available_at,
                                    m.calendar_available_at, m.as_of))
            or not m.version_available_at <= m.delivered_at <= m.as_of
            or m.calendar_available_at > m.as_of):
        raise ValueError("UNAVAILABLE_REVISION")

    a = m.instrument
    u = m.membership
    if (not isinstance(a, Instrument) or not _valid_id(a.asset_id)
            or a.kind not in ("STOCK", "ETF") or not _valid_id(a.venue)
            or not _date(a.listed_on)
            or (a.delisted_on is not None and
                (not _date(a.delisted_on) or a.delisted_on < a.listed_on))
            or not isinstance(a.symbols, tuple) or not a.symbols):
        raise ValueError("INVALID_INSTRUMENT")
    # No independent per-event availability receipt is present. Effective
    # symbol/delisting dates beyond this ET cutoff would disclose future
    # security-master information to a historical research snapshot.
    as_of_day = m.as_of.astimezone(_ET).date()
    if a.delisted_on is not None and a.delisted_on > as_of_day:
        raise ValueError("FUTURE_INSTRUMENT_EVENT")
    last_end = None
    for i, period in enumerate(a.symbols):
        if (not isinstance(period, SymbolPeriod)
                or not isinstance(period.symbol, str) or not _SYMBOL.fullmatch(period.symbol)
                or not _date(period.start)
                or (period.end is not None and
                    (not _date(period.end) or period.end < period.start))
                or (i == 0 and period.start != a.listed_on)
                or (i > 0 and (last_end is None or period.start <= last_end))):
            raise ValueError("INVALID_SYMBOL_HISTORY")
        if period.start > as_of_day or (period.end is not None and period.end > as_of_day):
            raise ValueError("FUTURE_INSTRUMENT_EVENT")
        last_end = period.end
    # An active security must retain an open-ended ticker interval. A closed
    # final symbol with no delisting event is an incomplete security master,
    # even when all provided historical bars precede that terminal date.
    if ((a.delisted_on is None and a.symbols[-1].end is not None)
            or (a.delisted_on is not None and a.symbols[-1].end != a.delisted_on)):
        raise ValueError("INVALID_SYMBOL_HISTORY")
    if (not isinstance(u, UniverseMembership) or not _date(u.day)
            or type(u.included) is not bool
            or not _aware(u.available_at) or u.available_at > m.as_of
            or (u.included and u.exclusion_reason is not None)
            or (not u.included and not _valid_id(u.exclusion_reason))
            or u.day < a.listed_on or
            (u.included and a.delisted_on is not None and u.day > a.delisted_on)):
        raise ValueError("INVALID_UNIVERSE_MEMBERSHIP")
    if (not isinstance(m.sessions, tuple) or not m.sessions
            or not isinstance(m.bar_days, tuple) or not m.bar_days
            or len(m.sessions) != len(m.bar_days)):
        raise ValueError("INCOMPLETE_SESSION_COVERAGE")
    previous_day = None
    for session, bar_day in zip(m.sessions, m.bar_days):
        if (not isinstance(session, ExchangeSession) or not _date(session.day)
                or not _date(bar_day) or
                (previous_day is not None and session.day <= previous_day)
                or session.day != bar_day):
            raise ValueError("INCOMPLETE_SESSION_COVERAGE")
        if (not _aware(session.opens_at) or not _aware(session.closes_at)
                or session.opens_at >= session.closes_at or
                session.opens_at.astimezone(_ET).date() != session.day or
                session.closes_at.astimezone(_ET).date() != session.day or
                session.opens_at.astimezone(_ET).time() != time(9, 30) or
                session.kind not in ("REGULAR", "EARLY_CLOSE") or
                (session.kind == "REGULAR" and
                 session.closes_at.astimezone(_ET).time() != time(16)) or
                (session.kind == "EARLY_CLOSE" and not
                 time(9, 30) < session.closes_at.astimezone(_ET).time() < time(16))):
            raise ValueError("UNSUPPORTED_SESSION")
        if (not a.listed_on <= session.day
                or (a.delisted_on is not None and session.day > a.delisted_on)
                or sum(p.start <= session.day and (p.end is None or session.day <= p.end)
                       for p in a.symbols) != 1):
            raise ValueError("MISSING_ASSET_HISTORY")
        previous_day = session.day
    # An artifact claiming to contain all completed daily sessions cannot
    # have been delivered before its final session closed. A purported early
    # delivery would falsely make future closing prices available at that time.
    # This chronological invariant is NOT vendor authentication.
    if m.delivered_at < m.sessions[-1].closes_at:
        raise ValueError("PREMATURE_ARTIFACT_DELIVERY")
    if (u.day > m.bar_days[0] or u.available_at >= m.sessions[0].opens_at
            or m.calendar_available_at >= m.sessions[0].opens_at
            or m.as_of < m.sessions[-1].closes_at):
        raise ValueError("POINT_IN_TIME_BOUNDARY")

    document = {
        "schema": 1,
        "instrument": {"id": a.asset_id, "kind": a.kind, "venue": a.venue,
                       "listed": a.listed_on.isoformat(),
                       "delisted": a.delisted_on.isoformat() if a.delisted_on else None,
                       "symbols": [{"symbol": p.symbol, "start": p.start.isoformat(),
                                    "end": p.end.isoformat() if p.end else None}
                                   for p in a.symbols]},
        "membership": {"day": u.day.isoformat(), "included": u.included,
                       "reason": u.exclusion_reason, "available": _utc(u.available_at)},
        "source": m.source_id, "feed": m.feed, "revision": m.revision,
        "license": m.license_grant_id, "authorized": m.authorized_for_local_research,
        "delivered": _utc(m.delivered_at), "version_available": _utc(m.version_available_at),
        "artifact_sha256": m.artifact_sha256,
        "calendar_source": m.calendar_source, "calendar_version": m.calendar_version,
        "calendar_sha256": m.calendar_sha256, "calendar_available": _utc(m.calendar_available_at),
        "sessions": [{"day": s.day.isoformat(), "open": _utc(s.opens_at),
                      "close": _utc(s.closes_at), "kind": s.kind} for s in m.sessions],
        "bar_days": [d.isoformat() for d in m.bar_days],
        "as_of": _utc(m.as_of),
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    digest = sha256(payload).hexdigest()
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise ValueError("MANIFEST_PIN_MISMATCH")
    # Compatibility is informational only. Historical security-master records
    # may use 11-15-character names, while paper risk and reservations allow
    # only ten. Long identifiers cannot be truncated or silently mapped.
    if not u.included:
        paper_symbol, paper_reason = None, "PAPER_UNIVERSE_EXCLUDED"
    elif a.delisted_on is not None and a.delisted_on <= as_of_day:
        paper_symbol, paper_reason = None, "PAPER_INSTRUMENT_DELISTED"
    else:
        active = [p.symbol for p in a.symbols
                  if p.start <= as_of_day and (p.end is None or as_of_day <= p.end)]
        if len(active) != 1:
            paper_symbol, paper_reason = None, "PAPER_NO_CURRENT_SYMBOL"
        elif _PAPER_SYMBOL.fullmatch(active[0]) is None:
            paper_symbol, paper_reason = None, "PAPER_SYMBOL_UNSUPPORTED_FORMAT"
        else:
            paper_symbol, paper_reason = active[0], "PAPER_SYMBOL_REPRESENTABLE_ONLY"
    return ManifestReceipt(m, digest, paper_symbol, paper_reason)
