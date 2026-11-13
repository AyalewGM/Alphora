"""Bounded, caller-attested US equities session-calendar consistency contract.

Research-only and SYNTHETIC-ONLY until independently sourced exchange calendars
are supplied off-repo. This module never accesses vendor/broker APIs, places
orders, or claims the accuracy of a calendar supplied by its caller.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_MIC = re.compile(r"[A-Z]{4}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = frozenset({"REGULAR", "EARLY_CLOSE", "CLOSED"})
_CLOSURES = frozenset({"WEEKEND", "HOLIDAY", "EMERGENCY_CLOSURE", "FULL_DAY_HALT"})


def _aware(value):
    return (type(value) is datetime and value.tzinfo is not None
            and value.utcoffset() is not None)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _local_at(value, day, clock):
    return (_aware(value) and value.astimezone(_ET).date() == day
            and value.astimezone(_ET).time().replace(tzinfo=None) == clock)


@dataclass(frozen=True)
class VenueHalt:
    begins_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class VenueDay:
    day: date
    kind: str
    opens_at: datetime | None
    closes_at: datetime | None
    information_available_at: datetime  # caller-attested, not authenticated
    closure_reason: str | None = None
    venue_halts: tuple[VenueHalt, ...] = ()


@dataclass(frozen=True)
class CalendarReceipt:
    venue_mic: str
    start: date
    end: date
    source: str
    revision: str
    license_claim: str
    as_of: datetime
    days: tuple[VenueDay, ...]
    sha256: str

    @property
    def session_days(self) -> tuple[date, ...]:
        """Optional input to corporate-action replay's expected_session_days."""
        return tuple(item.day for item in self.days if item.kind != "CLOSED")


def inspect_session_calendar(days, *, venue_mic, start, end, source, revision,
                             license_claim, as_of, expected_sha256=None):
    """Require complete daily coverage and an independently pinnable byte receipt.

    A caller's timestamps, closure claims and SHA-256 do NOT authenticate a
    vendor, licensing, a venue's official holidays, or retroactive corrections.
    The caller must independently verify source/version and decision-time
    availability before using this calendar for a historical strategy claim.
    """
    if (type(start) is not date or type(end) is not date or start > end
            or (end - start).days >= 366
            or not isinstance(venue_mic, str) or not _MIC.fullmatch(venue_mic)
            or not all(isinstance(x, str) and _ID.fullmatch(x)
                       for x in (source, revision, license_claim))
            or not _aware(as_of) or as_of.astimezone(_ET).date() < end
            or type(days) not in (list, tuple)
            or len(days) != (end - start).days + 1
            or (expected_sha256 is not None and
                (not isinstance(expected_sha256, str)
                 or not _SHA.fullmatch(expected_sha256)))):
        raise ValueError("INVALID_CALENDAR_METADATA")

    records = []
    for offset, item in enumerate(days):
        day = start + timedelta(days=offset)
        if (not isinstance(item, VenueDay) or type(item.day) is not date
                or item.day != day or type(item.kind) is not str
                or item.kind not in _KINDS
                or not _aware(item.information_available_at)
                or item.information_available_at > as_of
                or type(item.venue_halts) is not tuple):
            raise ValueError("INVALID_CALENDAR_DAY")
        closed = item.kind == "CLOSED"
        if closed:
            if (item.opens_at is not None or item.closes_at is not None
                    or item.venue_halts
                    or type(item.closure_reason) is not str
                    or item.closure_reason not in _CLOSURES
                    or (day.weekday() >= 5 and item.closure_reason != "WEEKEND")
                    or (day.weekday() < 5 and item.closure_reason == "WEEKEND")):
                raise ValueError("INVALID_CLOSED_SESSION")
        else:
            if (day.weekday() >= 5 or item.closure_reason is not None
                    or not _local_at(item.opens_at, day, time(9, 30))
                    or not _local_at(item.closes_at, day,
                                     time(13) if item.kind == "EARLY_CLOSE" else time(16))
                    or item.opens_at >= item.closes_at):
                raise ValueError("INVALID_TRADING_SESSION")
        halts = []
        previous_end = None
        for halt in item.venue_halts:
            if (not isinstance(halt, VenueHalt)
                    or not _aware(halt.begins_at) or not _aware(halt.ends_at)
                    or not item.opens_at <= halt.begins_at < halt.ends_at <= item.closes_at
                    or (previous_end is not None and halt.begins_at < previous_end)):
                raise ValueError("INVALID_VENUE_HALT")
            previous_end = halt.ends_at
            halts.append({"begins_utc": _utc(halt.begins_at),
                          "ends_utc": _utc(halt.ends_at)})
        records.append({"day": day.isoformat(), "kind": item.kind,
                        "opens_utc": _utc(item.opens_at) if not closed else None,
                        "closes_utc": _utc(item.closes_at) if not closed else None,
                        "information_available_utc": _utc(item.information_available_at),
                        "closure_reason": item.closure_reason, "venue_halts": halts})
    data = {"schema": 1, "kind": "caller_attested_venue_session_calendar",
            "venue_mic": venue_mic, "start": start.isoformat(),
            "end": end.isoformat(), "source": source, "revision": revision,
            "license_claim": license_claim, "as_of_utc": _utc(as_of),
            "days": records}
    digest = sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("ascii")).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError("CALENDAR_HASH_MISMATCH")
    return CalendarReceipt(venue_mic, start, end, source, revision, license_claim,
                           as_of, tuple(days), digest)


def require_exact_session_coverage(receipt, observed_days):
    """Reject missing/duplicate/closed-date observations, without guessing days."""
    if (not isinstance(receipt, CalendarReceipt)
            or type(observed_days) not in (tuple, list)
            or any(type(day) is not date for day in observed_days)
            or tuple(observed_days) != receipt.session_days):
        raise ValueError("SESSION_CALENDAR_MISMATCH")
    return receipt.session_days
