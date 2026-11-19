"""Synthetic-only cross-receipt consistency tests; no data vendor or network."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from alphora.research.manifest_calendar_binding import bind_manifest_calendar
from alphora.research.market_manifest import (
    ExchangeSession, Instrument, MarketManifest, SymbolPeriod,
    UniverseMembership, inspect_market_manifest,
)
from alphora.research.session_calendar import (
    VenueDay, VenueHalt, inspect_session_calendar,
)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
FIRST = date(2025, 11, 26)
CLOSED = date(2025, 11, 27)
LAST = date(2025, 11, 28)
ARTIFACT_SHA = "a" * 64
AVAILABLE = datetime(2025, 11, 24, tzinfo=UTC)
CALENDAR_CUTOFF = datetime(2025, 11, 25, tzinfo=UTC)
AS_OF = datetime(2025, 11, 30, tzinfo=UTC)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def calendar(*, venue="XNYS", source="synthetic_calendar", revision="cal-v1",
             as_of=CALENDAR_CUTOFF,
             day_available=AVAILABLE, first_halt=False):
    first = VenueDay(
        FIRST, "REGULAR", at(FIRST, 9, 30), at(FIRST, 16),
        day_available,
        venue_halts=(
            (VenueHalt(at(FIRST, 10), at(FIRST, 10, 15)),)
            if first_halt else ()
        ),
    )
    records = (
        first,
        VenueDay(CLOSED, "CLOSED", None, None, day_available,
                 closure_reason="HOLIDAY"),
        VenueDay(LAST, "EARLY_CLOSE", at(LAST, 9, 30),
                 at(LAST, 13), day_available),
    )
    return inspect_session_calendar(
        records, venue_mic=venue, start=FIRST, end=LAST,
        source=source, revision=revision, license_claim="synthetic",
        as_of=as_of,
    )


def manifest(c, *, venue="XNYS", source="synthetic_calendar",
             revision="cal-v1", regular_final=False):
    sessions = (
        ExchangeSession(FIRST, at(FIRST, 9, 30), at(FIRST, 16), "REGULAR"),
        ExchangeSession(LAST, at(LAST, 9, 30),
                        at(LAST, 16) if regular_final else at(LAST, 13),
                        "REGULAR" if regular_final else "EARLY_CLOSE"),
    )
    m = MarketManifest(
        schema_version=1,
        instrument=Instrument(
            "stable-asset-id", "ETF", venue, date(2020, 1, 1), None,
            (SymbolPeriod("SYNTH", date(2020, 1, 1)),),
        ),
        membership=UniverseMembership(
            date(2025, 11, 25), True, None,
            datetime(2025, 11, 24, 12, tzinfo=UTC),
        ),
        source_id="synthetic_vendor", feed="daily_raw",
        revision="version_1", license_grant_id="synthetic_only",
        authorized_for_local_research=True,
        delivered_at=datetime(2025, 11, 29, tzinfo=UTC),
        version_available_at=datetime(2025, 11, 28, 21, tzinfo=UTC),
        artifact_sha256=ARTIFACT_SHA,
        calendar_source=source, calendar_version=revision,
        calendar_sha256=c.sha256,
        calendar_available_at=CALENDAR_CUTOFF,
        sessions=sessions, bar_days=(FIRST, LAST), as_of=AS_OF,
    )
    return inspect_market_manifest(
        m, expected_artifact_sha256=ARTIFACT_SHA,
        expected_calendar_sha256=c.sha256,
    )


def bind(m, c, **kwargs):
    params = {
        "expected_artifact_sha256": ARTIFACT_SHA,
        "expected_calendar_sha256": c.sha256,
        # Self-pinned synthetic fixture; production pins must be supplied
        # independently of the receipt being checked.
        "expected_manifest_sha256": m.sha256,
    }
    params.update(kwargs)
    return bind_manifest_calendar(m, c, **params)


class ManifestCalendarBindingTests(unittest.TestCase):
    def test_repeatable_synthetic_binding_does_not_infer_vendor_authenticity(self):
        c = calendar()
        m = manifest(c)
        result = bind(m, c, expected_manifest_sha256=m.sha256)
        self.assertEqual(result, bind(m, c))
        self.assertEqual(result.session_days, (FIRST, LAST))
        self.assertEqual(result.manifest_sha256, m.sha256)
        self.assertEqual(result.calendar_sha256, c.sha256)
        self.assertEqual(len(result.binding_sha256), 64)
        for name in ("trade", "place_order", "fetch_vendor", "broker"):
            self.assertFalse(hasattr(result, name))

    def test_mismatched_early_close_is_rejected_even_with_matching_calendar_hash(self):
        c = calendar()
        m = manifest(c, regular_final=True)
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_SESSION_MISMATCH"):
            bind(m, c)

    def test_calendar_venue_or_revision_cannot_silently_substitute(self):
        for changed in (
            calendar(venue="XNAS"),
            calendar(source="different_source"),
            calendar(revision="cal-v2"),
        ):
            with self.subTest(changed=changed.sha256):
                m = manifest(changed)  # Keep all external pins consistent.
                with self.assertRaisesRegex(
                    ValueError, "MANIFEST_CALENDAR_IDENTITY_MISMATCH"
                ):
                    bind(m, changed)

    def test_later_declared_calendar_information_is_lookahead(self):
        changed = calendar(
            day_available=CALENDAR_CUTOFF + timedelta(hours=1),
            as_of=CALENDAR_CUTOFF + timedelta(hours=2),
        )
        m = manifest(changed)
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_LOOKAHEAD"):
            bind(m, changed)

    def test_retrospective_calendar_snapshot_cannot_borrow_early_day_timestamps(self):
        # Day records claim November 24 availability, but the snapshot itself
        # is dated after the November 26 first trading session.
        late = calendar(as_of=datetime(2025, 11, 29, tzinfo=UTC))
        m = manifest(late)
        self.assertGreater(late.as_of, m.manifest.calendar_available_at)
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_LOOKAHEAD"):
            bind(m, late)

    def test_advance_published_schedule_can_cover_later_sessions(self):
        early = calendar(as_of=CALENDAR_CUTOFF)
        self.assertLess(early.as_of.date(), LAST)
        self.assertEqual(bind(manifest(early), early).session_days, (FIRST, LAST))

    def test_post_manifest_calendar_snapshot_is_not_historical_evidence(self):
        changed = calendar(as_of=AS_OF + timedelta(hours=1))
        m = manifest(changed)
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_LOOKAHEAD"):
            bind(m, changed)

    def test_false_or_mutated_receipt_hash_is_rechecked(self):
        c = calendar()
        m = manifest(c)
        with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"):
            bind(replace(m, sha256="0" * 64), c)
        with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"):
            bind(m, replace(c, sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_EXTERNAL_PIN_MISMATCH"):
            bind(m, c, expected_manifest_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"):
            bind(m, c, expected_artifact_sha256="f" * 64)

    def test_forged_paper_symbol_fields_rejected(self):
        # A receipt's derived fields must agree with freshly recomputed
        # manifest interpretation; matching content hashes are insufficient.
        c = calendar()
        original = manifest(c)
        self.assertEqual(original.paper_symbol, "SYNTH")
        for forged in (
            replace(original, paper_symbol="OTHER"),
            replace(original, paper_symbol=None),
            replace(original, paper_symbol_reason="FORGED_PAPER_PERMISSION"),
        ):
            with self.subTest(forged=forged):
                with self.assertRaisesRegex(
                    ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"
                ):
                    bind(forged, c)

    def test_unrepresentable_intraday_halt_is_not_silently_ignored(self):
        c = calendar(first_halt=True)
        m = manifest(c)
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_UNSUPPORTED_HALT"):
            bind(m, c)

    def test_missing_and_extra_sessions_fail_closed(self):
        c = calendar()
        m = manifest(c)
        for changed in (
            replace(m.manifest, bar_days=(FIRST,)),
            replace(m.manifest, sessions=m.manifest.sessions[::-1]),
        ):
            with self.subTest(changed=changed):
                bad = replace(m, manifest=changed)
                with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"):
                    bind(bad, c)

    def test_valid_recomputed_manifest_cannot_replace_external_pin(self):
        c = calendar()
        original = manifest(c)
        replaced_manifest = replace(original.manifest, source_id="different_source")
        substituted = inspect_market_manifest(
            replaced_manifest,
            expected_artifact_sha256=ARTIFACT_SHA,
            expected_calendar_sha256=c.sha256,
        )
        self.assertNotEqual(original.sha256, substituted.sha256)
        with self.assertRaisesRegex(
            ValueError, "MANIFEST_CALENDAR_EXTERNAL_PIN_MISMATCH"
        ):
            bind(substituted, c, expected_manifest_sha256=original.sha256)

    def test_external_manifest_pin_is_required_even_for_synthetic_receipts(self):
        c = calendar()
        m = manifest(c)
        # Previously an omitted external pin let a valid self-computed
        # manifest receipt pass without independently pinned identity.
        with self.assertRaises(TypeError):
            bind_manifest_calendar(
                m, c,
                expected_artifact_sha256=ARTIFACT_SHA,
                expected_calendar_sha256=c.sha256,
            )
        with self.assertRaisesRegex(
            ValueError, "MANIFEST_CALENDAR_EXTERNAL_PIN_MISMATCH"
        ):
            bind(m, c, expected_manifest_sha256=None)

    def test_wrong_objects_are_rejected(self):
        c = calendar()
        m = manifest(c)
        with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"):
            bind_manifest_calendar(
                None, c,
                expected_artifact_sha256=ARTIFACT_SHA,
                expected_calendar_sha256=c.sha256,
                expected_manifest_sha256=m.sha256,
            )
        with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_CALENDAR_RECEIPTS"):
            bind_manifest_calendar(
                m, None,
                expected_artifact_sha256=ARTIFACT_SHA,
                expected_calendar_sha256=c.sha256,
                expected_manifest_sha256=m.sha256,
            )


    def test_dst_fold_snapshot_cannot_evade_pre_session_cutoff(self):
        early = datetime(2025, 11, 2, 1, 50, tzinfo=ET, fold=0)
        late = datetime(2025, 11, 2, 1, 20, tzinfo=ET, fold=1)
        self.assertLess(early.astimezone(UTC), late.astimezone(UTC))
        # Day facts are genuinely earlier, but the calendar snapshot
        # itself was only first seen after the pinned manifest cutoff.
        c = calendar(as_of=late, day_available=early)
        base = manifest(c)
        altered = replace(base.manifest, calendar_available_at=early)
        m = inspect_market_manifest(
            altered, expected_artifact_sha256=ARTIFACT_SHA,
            expected_calendar_sha256=c.sha256)
        with self.assertRaisesRegex(ValueError, "MANIFEST_CALENDAR_LOOKAHEAD"):
            bind(m, c)

        # Positive control: a snapshot published earlier in absolute time
        # than the declared calendar cutoff is structurally acceptable.
        c2 = calendar(as_of=early, day_available=early)
        base2 = manifest(c2)
        altered2 = replace(base2.manifest, calendar_available_at=late)
        m2 = inspect_market_manifest(
            altered2, expected_artifact_sha256=ARTIFACT_SHA,
            expected_calendar_sha256=c2.sha256)
        self.assertEqual(bind(m2, c2).session_days, (FIRST, LAST))

    def test_dst_fold_market_version_delivery_order_is_absolute(self):
        early = datetime(2025, 11, 2, 1, 50, tzinfo=ET, fold=0)
        late = datetime(2025, 11, 2, 1, 20, tzinfo=ET, fold=1)
        first, last = date(2025, 10, 30), date(2025, 10, 31)
        c = calendar()
        base = manifest(c).manifest
        data = replace(
            base,
            membership=UniverseMembership(
                date(2025, 10, 29), True, None,
                datetime(2025, 10, 29, tzinfo=UTC)),
            sessions=(
                ExchangeSession(first, at(first, 9, 30), at(first, 16), "REGULAR"),
                ExchangeSession(last, at(last, 9, 30), at(last, 16), "REGULAR"),
            ),
            bar_days=(first, last),
            calendar_available_at=datetime(2025, 10, 29, tzinfo=UTC),
            as_of=datetime(2025, 11, 3, tzinfo=UTC),
            delivered_at=early,
            version_available_at=late,
        )
        # A revision first available at 06:20 UTC cannot have been
        # delivered at 05:50 UTC, even if 01:20 < 01:50 on the wall.
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_REVISION"):
            inspect_market_manifest(
                data, expected_artifact_sha256=ARTIFACT_SHA,
                expected_calendar_sha256=c.sha256)
        # Reverse order is valid in UTC despite reversed local wall clocks.
        valid = replace(data, version_available_at=early, delivered_at=late)
        receipt = inspect_market_manifest(
            valid, expected_artifact_sha256=ARTIFACT_SHA,
            expected_calendar_sha256=c.sha256)
        self.assertEqual(receipt.manifest.delivered_at, late)


if __name__ == "__main__":
    unittest.main()
