"""Offline, point-in-time catalyst evidence assessment.

This module never fetches URLs or places orders. A vendor adapter must provide
trustworthy *first observed* timestamps and source-origin attestations; article
publication time alone cannot establish that information was tradeable.
"""
from dataclasses import dataclass
from datetime import datetime
import re
from urllib.parse import urlsplit

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CATEGORIES = frozenset({
    "EARNINGS", "GUIDANCE", "MERGER", "REGULATORY", "ANALYST",
    "FINANCING", "SECTOR", "PROMOTION", "OTHER",
})
_SOURCES = frozenset({"SEC_FILING", "COMPANY_RELEASE", "PROFESSIONAL_NEWS", "SOCIAL", "OTHER"})
_DIRECTIONS = frozenset({"POSITIVE", "NEGATIVE", "MIXED", "UNKNOWN"})
_PRIMARY = frozenset({"SEC_FILING", "COMPANY_RELEASE"})


def _aware(value: object) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _safe_https_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and parts.port in (None, 443) and not parts.fragment)
    except ValueError:
        return False


@dataclass(frozen=True)
class CatalystEvent:
    """Immutable event metadata; provenance fields must not be LLM-generated."""
    event_id: str
    symbol: str
    category: str
    source_kind: str
    direction: str
    source_url: str
    publisher: str
    published_at: datetime
    first_seen_at: datetime
    received_at: datetime
    source_origin_verified: bool
    promotional: bool | None = None


@dataclass(frozen=True)
class EvidenceItem:
    event_id: str
    category: str
    direction: str
    source_url: str
    first_available_at: datetime
    primary_source_attested: bool


@dataclass(frozen=True)
class CatalystAssessment:
    symbol: str
    as_of: datetime
    classification: str  # PRIMARY_EVENT, UNVERIFIED_EVENT, REVIEW_REQUIRED, NO_TIMELY_EVENT
    evidence: tuple[EvidenceItem, ...]
    reasons: tuple[str, ...]
    # Never promoted into the decision engine's 'VERIFIED' gate by this module.
    entry_catalyst_status: str = "REVIEW_REQUIRED"


def _valid_event(event: CatalystEvent, symbol: str) -> bool:
    return (
        isinstance(event, CatalystEvent)
        and isinstance(event.event_id, str) and bool(_EVENT_ID.fullmatch(event.event_id))
        and event.symbol == symbol
        and isinstance(event.category, str) and event.category in _CATEGORIES
        and isinstance(event.source_kind, str) and event.source_kind in _SOURCES
        and isinstance(event.direction, str) and event.direction in _DIRECTIONS
        and _safe_https_url(event.source_url)
        and isinstance(event.publisher, str)
        and 0 < len(event.publisher) <= 255
        and not event.publisher.isspace()
        and all(ord(c) >= 32 for c in event.publisher)
        and all(_aware(t) for t in (event.published_at, event.first_seen_at, event.received_at))
        and type(event.source_origin_verified) is bool
        and event.promotional in (True, False, None)
        and (event.promotional is None or type(event.promotional) is bool)
        and event.published_at <= event.first_seen_at <= event.received_at
    )


def assess_catalysts(symbol: str, *, as_of: datetime,
                     previous_session_close_at: datetime,
                     events: list[CatalystEvent] | tuple[CatalystEvent, ...]) -> CatalystAssessment:
    """Assess known-at-decision events without causal claims or trade approval.

    Invalid or contradictory data forces review, future/previous-session events
    are excluded, syndication duplicates are deduplicated by source event ID.
    """
    if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
            or not _aware(as_of) or not _aware(previous_session_close_at)
            or previous_session_close_at >= as_of
            or not isinstance(events, (list, tuple))):
        raise ValueError("INVALID_ASSESSMENT_INPUT")

    reasons: set[str] = set()
    selected: dict[str, CatalystEvent] = {}
    for event in events:
        if not _valid_event(event, symbol):
            reasons.add("INVALID_OR_UNVERIFIED_METADATA")
            continue
        if event.received_at > as_of:
            reasons.add("FUTURE_INFORMATION_EXCLUDED")
            continue
        previous = selected.get(event.event_id)
        if previous is not None and previous != event:
            # Compare all revisions known by as_of before age filtering.
            # Contradictory historical timestamps must never silently disappear.
            reasons.add("CONFLICTING_EVENT_ID")
        else:
            selected[event.event_id] = event

    timely = {}
    for event_id, event in selected.items():
        if event.published_at <= previous_session_close_at:
            reasons.add("OLDER_THAN_PREVIOUS_CLOSE")
            continue
        timely[event_id] = event
    selected = timely

    evidence: list[EvidenceItem] = []
    for event in selected.values():
        if event.promotional is True or event.category == "PROMOTION":
            reasons.add("PROMOTIONAL_ACTIVITY_REVIEW")
        if event.source_origin_verified is not True:
            reasons.add("SOURCE_NOT_ATTESTED")
        if event.direction in ("NEGATIVE", "MIXED", "UNKNOWN"):
            reasons.add("DIRECTION_NOT_UNAMBIGUOUS")
        evidence.append(EvidenceItem(
            event_id=event.event_id,
            category=event.category,
            direction=event.direction,
            source_url=event.source_url,
            first_available_at=event.received_at,
            primary_source_attested=(event.source_kind in _PRIMARY
                                     and event.source_origin_verified is True),
        ))

    evidence.sort(key=lambda item: (item.first_available_at, item.event_id))
    directions = {item.direction for item in evidence}
    if "POSITIVE" in directions and "NEGATIVE" in directions:
        reasons.add("CONFLICTING_EVENT_DIRECTIONS")

    hard_review = bool(reasons.intersection({
        "INVALID_OR_UNVERIFIED_METADATA", "CONFLICTING_EVENT_ID",
        "PROMOTIONAL_ACTIVITY_REVIEW", "SOURCE_NOT_ATTESTED",
        "DIRECTION_NOT_UNAMBIGUOUS", "CONFLICTING_EVENT_DIRECTIONS",
    }))
    if hard_review:
        classification = "REVIEW_REQUIRED"
    elif any(item.primary_source_attested for item in evidence):
        classification = "PRIMARY_EVENT"
    elif evidence:
        classification = "UNVERIFIED_EVENT"
    else:
        classification = "NO_TIMELY_EVENT"
    return CatalystAssessment(symbol, as_of, classification, tuple(evidence),
                              tuple(sorted(reasons)))
