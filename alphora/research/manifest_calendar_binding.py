"""Cross-check independently pinned, *caller-attested* market and calendar receipts.

Research-only, offline validation. It neither authenticates vendor statements
nor verifies actual exchange hours, licensing, historical revisions or bar bytes.
It performs no IO, broker actions, orders, or network access.
"""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json

from .market_manifest import ManifestReceipt, inspect_market_manifest
from .session_calendar import CalendarReceipt, require_exact_session_coverage


@dataclass(frozen=True)
class ManifestCalendarBinding:
    """Local content fingerprint tying one market manifest to one calendar."""

    manifest_sha256: str
    calendar_sha256: str
    asset_id: str
    venue_mic: str
    session_days: tuple[date, ...]
    binding_sha256: str


def bind_manifest_calendar(
    manifest_receipt: ManifestReceipt,
    calendar_receipt: CalendarReceipt,
    *,
    expected_artifact_sha256: str,
    expected_calendar_sha256: str,
    expected_manifest_sha256: str | None = None,
) -> ManifestCalendarBinding:
    """Refuse inconsistent receipts before any offline research reuse.

    All expected hashes are externally supplied pins, NOT signatures or
    independently verified source authority. Revalidate both *contents*,
    then bind venue, calendar identity, declared availability and exact
    exchange-session boundaries. A matching hash cannot prove a vendor
    provided an authentic, licensed, point-in-time dataset.
    """
    if (type(manifest_receipt) is not ManifestReceipt
            or type(calendar_receipt) is not CalendarReceipt):
        raise ValueError("INVALID_MANIFEST_CALENDAR_RECEIPTS")
    m = manifest_receipt.manifest
    c = calendar_receipt

    try:
        verified = inspect_market_manifest(
            m,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_calendar_sha256=expected_calendar_sha256,
            expected_manifest_sha256=manifest_receipt.sha256,
        )
        require_exact_session_coverage(c, m.bar_days)
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("INVALID_MANIFEST_CALENDAR_RECEIPTS") from exc

    if expected_manifest_sha256 is not None and verified.sha256 != expected_manifest_sha256:
        raise ValueError("MANIFEST_CALENDAR_EXTERNAL_PIN_MISMATCH")
    if c.sha256 != m.calendar_sha256 or c.sha256 != expected_calendar_sha256:
        raise ValueError("MANIFEST_CALENDAR_EXTERNAL_PIN_MISMATCH")
    if (c.venue_mic != m.instrument.venue or c.source != m.calendar_source
            or c.revision != m.calendar_version):
        raise ValueError("MANIFEST_CALENDAR_IDENTITY_MISMATCH")
    if c.start != m.bar_days[0] or c.end != m.bar_days[-1]:
        raise ValueError("MANIFEST_CALENDAR_RANGE_MISMATCH")
    if c.as_of > m.as_of:
        raise ValueError("MANIFEST_CALENDAR_LOOKAHEAD")
    # The manifest claims the calendar was available strictly before its
    # first bar's session. Do not silently accept calendar records that were
    # *claimed* to have arrived only after that cutoff.
    if any(day.information_available_at > m.calendar_available_at for day in c.days):
        raise ValueError("MANIFEST_CALENDAR_LOOKAHEAD")

    sessions = tuple(day for day in c.days if day.kind != "CLOSED")
    if len(sessions) != len(m.sessions) or any(
        actual.day != declared.day
        or actual.kind != declared.kind
        or actual.opens_at != declared.opens_at
        or actual.closes_at != declared.closes_at
        for actual, declared in zip(sessions, m.sessions)
    ):
        raise ValueError("MANIFEST_CALENDAR_SESSION_MISMATCH")

    document = {
        "schema": 1,
        "kind": "caller_attested_manifest_calendar_binding",
        "manifest_sha256": verified.sha256,
        "calendar_sha256": c.sha256,
        "asset_id": m.instrument.asset_id,
        "venue_mic": m.instrument.venue,
        "start": c.start.isoformat(),
        "end": c.end.isoformat(),
    }
    digest = sha256(json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")).hexdigest()
    return ManifestCalendarBinding(
        manifest_sha256=verified.sha256,
        calendar_sha256=c.sha256,
        asset_id=m.instrument.asset_id,
        venue_mic=m.instrument.venue,
        session_days=c.session_days,
        binding_sha256=digest,
    )
