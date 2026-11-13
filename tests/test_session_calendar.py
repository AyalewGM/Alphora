"""All data here are invented fixtures, not an official exchange calendar."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alphora.research.session_calendar import (
    VenueDay, VenueHalt, inspect_session_calendar, require_exact_session_coverage,
)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
START = date(2026, 11, 2)
END = date(2026, 11, 8)
AS_OF = datetime(2026, 11, 9, tzinfo=UTC)
AVAILABLE = datetime(2026, 11, 1, tzinfo=UTC)


def at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(
        hour=hour, minute=minute)


def sample():
    records = []
    for offset in range((END - START).days + 1):
        d = START + timedelta(days=offset)
        if d.weekday() >= 5:
            records.append(VenueDay(d, "CLOSED", None, None, AVAILABLE,
                                    closure_reason="WEEKEND"))
        elif d == date(2026, 11, 4):
            # An INVENTED midweek closure. Do not infer true holidays from it.
            records.append(VenueDay(d, "CLOSED", None, None, AVAILABLE,
                                    closure_reason="HOLIDAY"))
        elif d == date(2026, 11, 6):
            # An INVENTED early close, not actual NYSE schedule data.
            records.append(VenueDay(d, "EARLY_CLOSE", at(d, 9, 30),
                                    at(d, 13), AVAILABLE))
        else:
            records.append(VenueDay(d, "REGULAR", at(d, 9, 30),
                                    at(d, 16), AVAILABLE))
    return tuple(records)


def inspect(records=None, **kwargs):
    params = dict(venue_mic="XNYS", start=START, end=END,
                  source="synthetic", revision="v1",
                  license_claim="synthetic", as_of=AS_OF)
    params.update(kwargs)
    return inspect_session_calendar(sample() if records is None else records,
                                    **params)


class SessionCalendarTests(unittest.TestCase):
    def test_full_coverage_and_early_close_is_pinned(self):
        result = inspect()
        self.assertEqual(result.session_days, (date(2026,11,2), date(2026,11,3),
                                               date(2026,11,5), date(2026,11,6)))
        self.assertEqual(len(result.sha256), 64)
        self.assertEqual(inspect(expected_sha256=result.sha256), result)
        self.assertEqual(require_exact_session_coverage(result, list(result.session_days)),
                         result.session_days)

    def test_missing_or_duplicate_bar_day_rejected(self):
        result = inspect()
        for candidate in (result.session_days[1:],
                          result.session_days + (result.session_days[-1],),
                          (START, date(2026,11,4)) + result.session_days[1:],
                          tuple(reversed(result.session_days)), ("2026-11-02",)):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "SESSION_CALENDAR_MISMATCH"):
                    require_exact_session_coverage(result, candidate)

    def test_missing_duplicate_reordered_calendar_days_rejected(self):
        good = sample()
        for data in (good[:-1], good + (good[-1],),
                     good[:2] + (good[0],) + good[3:]):
            with self.subTest(size=len(data)):
                with self.assertRaises(ValueError):
                    inspect(data)

    def test_weekend_must_be_closed_and_weekday_cannot_be_weekend(self):
        good = list(sample())
        for index, alteration in ((5, replace(good[5], kind="REGULAR",
                                               opens_at=at(good[5].day, 9, 30),
                                               closes_at=at(good[5].day,16),
                                               closure_reason=None)),
                                  (2, replace(good[2], closure_reason="WEEKEND"))):
            changed = good[:]
            changed[index] = alteration
            with self.assertRaises(ValueError):
                inspect(changed)

    def test_early_close_and_new_york_dst_offsets_checked(self):
        good = list(sample())
        original = good[4]
        for close in (at(original.day, 16), at(original.day, 12),
                      datetime(2026,11,6,13,tzinfo=UTC)):
            changed = good[:]
            changed[4] = replace(original, closes_at=close)
            with self.subTest(close=close), self.assertRaisesRegex(
                    ValueError, "INVALID_TRADING_SESSION"):
                inspect(changed)
        # New York changes DST on Nov 1. Nov 2 09:30 ET is 14:30 UTC.
        self.assertEqual(good[0].opens_at.astimezone(UTC).hour, 14)

    def test_overlapping_or_outside_session_halts_rejected(self):
        good = list(sample())
        first = good[0]
        valid = VenueHalt(at(first.day, 10), at(first.day, 10, 15))
        second = VenueHalt(at(first.day, 10, 15), at(first.day, 10, 30))
        good[0] = replace(first, venue_halts=(valid,second))
        self.assertNotEqual(inspect(good).sha256, inspect().sha256)
        for extra in (VenueHalt(at(first.day, 10, 14), at(first.day, 11)),
                      VenueHalt(at(first.day, 16), at(first.day, 17)),
                      VenueHalt(at(first.day, 10), at(first.day, 10))):
            bad = good[:]
            bad[0] = replace(first, venue_halts=(valid,extra))
            with self.subTest(extra=extra), self.assertRaisesRegex(
                    ValueError, "INVALID_VENUE_HALT"):
                inspect(bad)

    def test_attested_availability_cutoff_and_tampered_hash_rejected(self):
        good = list(sample())
        good[0] = replace(good[0], information_available_at=AS_OF + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "INVALID_CALENDAR_DAY"):
            inspect(good)
        with self.assertRaisesRegex(ValueError, "CALENDAR_HASH_MISMATCH"):
            inspect(expected_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "INVALID_CALENDAR_METADATA"):
            inspect(as_of=datetime(2026,11,7,tzinfo=UTC))
        with self.assertRaisesRegex(ValueError, "INVALID_CALENDAR_METADATA"):
            inspect(start=END, end=START)

    def test_revisions_change_hash_without_claiming_source_truth(self):
        original = inspect()
        self.assertNotEqual(original.sha256, inspect(revision="v2").sha256)
        self.assertEqual(original.sha256, inspect().sha256)
        self.assertNotEqual(original.sha256, inspect(source="another-source").sha256)

    def test_closed_session_has_no_marks_or_halts(self):
        good = list(sample())
        i = 2
        good[i] = replace(good[i], venue_halts=(VenueHalt(at(good[i].day,10),
                                                        at(good[i].day,11)),))
        with self.assertRaisesRegex(ValueError, "INVALID_CLOSED_SESSION"):
            inspect(good)

    def test_malformed_unhashable_kind_or_reason_fails_closed(self):
        good = list(sample())
        good[0] = replace(good[0], kind=["REGULAR"])
        with self.assertRaisesRegex(ValueError, "INVALID_CALENDAR_DAY"):
            inspect(good)
        good = list(sample())
        good[2] = replace(good[2], closure_reason=["HOLIDAY"])
        with self.assertRaisesRegex(ValueError, "INVALID_CLOSED_SESSION"):
            inspect(good)


if __name__ == "__main__":
    unittest.main()
