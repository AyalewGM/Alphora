"""Synthetic offline provenance tests; no external feed, networking or trades."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
from zoneinfo import ZoneInfo

from alphora.research.backtest import Bar
from alphora.research.data_provenance import ObservedDailyBar, inspect_daily_snapshot

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)


def observed(day: int, *, close="100", first_minutes=5, revised_minutes=10):
    d = date(2026, 11, day)
    end = datetime(2026, 11, day, 16, 0, tzinfo=ET)
    return ObservedDailyBar(
        Bar(d, D("100"), D("105"), D("95"), D(close), 1000000),
        end + timedelta(minutes=first_minutes),
        end + timedelta(minutes=revised_minutes),
    )


def inspect(*records, **options):
    settings = dict(
        asset_type="ETF", source="synthetic_vendor",
        source_revision="batch-20261104", price_basis="RAW_UNADJUSTED",
        as_of=AS_OF,
    )
    settings.update(options)
    return inspect_daily_snapshot("SPY", records, **settings)


class PointInTimeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.bars = (observed(2), observed(3))

    def test_valid_snapshot_is_immutable_with_canonical_hash(self):
        receipt = inspect(*self.bars)
        self.assertEqual(receipt.bars, tuple(x.bar for x in self.bars))
        self.assertEqual(len(receipt.sha256), 64)
        self.assertEqual(receipt, inspect(*self.bars))
        with self.assertRaises(Exception):
            receipt.sha256 = "changed"

    def test_reproducible_across_input_list_and_timezones(self):
        a, b = self.bars
        converted = replace(a,
                            first_seen_at=a.first_seen_at.astimezone(timezone.utc),
                            version_available_at=a.version_available_at.astimezone(timezone.utc))
        self.assertEqual(inspect(a, b).sha256,
                         inspect(converted, b, as_of=AS_OF.astimezone(ET)).sha256)
        self.assertEqual(inspect(a, b).sha256, inspect(*list(self.bars)).sha256)

    def test_hash_changes_for_actual_data_version(self):
        a, b = self.bars
        self.assertNotEqual(inspect(a, b).sha256,
                            inspect(a, replace(b, bar=replace(b.bar, close=D("101")))).sha256)
        self.assertNotEqual(inspect(a, b).sha256,
                            inspect(a, replace(b, version_available_at=b.version_available_at +
                                               timedelta(minutes=1))).sha256)

    def test_tiny_vendor_revisions_have_distinct_hashes_at_any_precision(self):
        # Both prices are valid synthetic bars, but normalize() at precision
        # 3 or 28 used to round them to the same canonical hash input.
        first, second = self.bars
        old_close = D("100.0000000000000000000000000000000001")
        revised_close = D("100.0000000000000000000000000000000002")
        earlier = replace(second, bar=replace(second.bar, close=old_close))
        revised = replace(second, bar=replace(second.bar, close=revised_close))
        equivalent = replace(second, bar=replace(
            second.bar, close=D("100.000000000000000000000000000000000100")))
        for precision in (3, 28, 80):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                pinned = inspect(first, earlier).sha256
                self.assertNotEqual(pinned, inspect(first, revised).sha256)
                self.assertEqual(pinned, inspect(first, equivalent).sha256)
                with self.assertRaisesRegex(ValueError, "SNAPSHOT_HASH_MISMATCH"):
                    inspect(first, revised, expected_sha256=pinned)

    def test_v2_receipt_requires_explicit_repin_of_v1_hashes(self):
        # Known synthetic fixture digests freeze the canonicalization schema.
        # A v1 receipt must be rejected rather than silently treated as v2.
        legacy_digest = "788f90d65cdd6820d694e69f69cc374a4f20eaa8d1930f2e19f3016ab4cea922"
        v2_digest = "37a502dbbf9bf3c37ea73c34db7534d54e9a545548e3e1087ebaad78743c6533"
        self.assertEqual(inspect(*self.bars).sha256, v2_digest)
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_HASH_MISMATCH"):
            inspect(*self.bars, expected_sha256=legacy_digest)

    def test_expected_hash_fails_closed(self):
        digest = inspect(*self.bars).sha256
        self.assertEqual(inspect(*self.bars, expected_sha256=digest).sha256, digest)
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_HASH_MISMATCH"):
            inspect(*self.bars, expected_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "INVALID_SNAPSHOT_METADATA"):
            inspect(*self.bars, expected_sha256="not-a-sha")

    def test_future_version_rejected_even_if_original_bar_was_known(self):
        a, b = self.bars
        poisoned = replace(a, version_available_at=AS_OF + timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
            inspect(poisoned, b)

    def test_dst_fold_uses_instants_for_version_availability(self):
        # A Friday bar can be revised over the DST fall-back weekend.
        a, b = self.bars
        first_close = datetime(2026, 10, 29, 16, 0, tzinfo=ET)
        a = replace(a, bar=replace(a.bar, day=first_close.date()),
                    first_seen_at=first_close + timedelta(minutes=5),
                    version_available_at=first_close + timedelta(minutes=10))
        b = replace(b, bar=replace(b.bar, day=date(2026, 10, 30)),
                    first_seen_at=datetime(2026, 10, 30, 16, 5, tzinfo=ET))
        early = datetime(2026, 11, 1, 1, 45, tzinfo=ET, fold=0)
        late = datetime(2026, 11, 1, 1, 15, tzinfo=ET, fold=1)
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
            inspect(a, replace(b, version_available_at=late), as_of=early)
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
            inspect(a, replace(b, first_seen_at=late, version_available_at=early),
                    as_of=late)
        valid = replace(b, first_seen_at=early, version_available_at=late)
        receipt = inspect(a, valid, as_of=late)
        utc_valid = replace(valid, first_seen_at=early.astimezone(timezone.utc),
                            version_available_at=late.astimezone(timezone.utc))
        self.assertEqual(receipt.sha256,
                         inspect(a, utc_valid, as_of=late.astimezone(timezone.utc)).sha256)
        same_wall_later = early.replace(fold=1)
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
            inspect(a, replace(b, version_available_at=same_wall_later), as_of=early)

    def test_revision_after_next_regular_open_is_lookahead(self):
        a, b = self.bars
        correction = replace(a, version_available_at=datetime(2026, 11, 3, 9, 31, tzinfo=ET))
        with self.assertRaisesRegex(ValueError, "NEXT_OPEN_LOOKAHEAD"):
            inspect(correction, b)

    def test_revision_exactly_at_next_open_is_not_available(self):
        a, b = self.bars
        correction = replace(a, version_available_at=datetime(2026, 11, 3, 9, 30, tzinfo=ET))
        with self.assertRaisesRegex(ValueError, "NEXT_OPEN_LOOKAHEAD"):
            inspect(correction, b)

    def test_revision_before_next_open_eligible(self):
        a, b = self.bars
        correction = replace(a, version_available_at=datetime(2026, 11, 3, 9, 29, tzinfo=ET))
        self.assertEqual(len(inspect(correction, b).bars), 2)

    def test_first_seen_before_regular_close_rejected(self):
        a, b = self.bars
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
            inspect(replace(a, first_seen_at=datetime(2026, 11, 2, 15, 59, tzinfo=ET)), b)

    def test_version_before_first_seen_rejected(self):
        a, b = self.bars
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
            inspect(replace(a, version_available_at=a.first_seen_at-timedelta(seconds=1)), b)

    def test_naive_or_non_datetime_attestations_rejected(self):
        a, b = self.bars
        for val in (datetime(2026, 11, 2, 16), "2026-11-02T16:05:00", None):
            with self.subTest(value=str(val)):
                with self.assertRaisesRegex(ValueError, "UNAVAILABLE_BAR_VERSION"):
                    inspect(replace(a, first_seen_at=val), b)

    def test_unsorted_duplicate_and_invalid_bar_rejected(self):
        a, b = self.bars
        for pair in ((b, a), (a, a), (replace(a, bar=replace(a.bar, volume=-1)), b)):
            with self.subTest(pair=pair):
                with self.assertRaisesRegex(ValueError, "INVALID_OBSERVATIONS"):
                    inspect(*pair)

    def test_untyped_records_and_short_data_rejected(self):
        for rows in ((self.bars[0],), (self.bars[0], "not a bar"), ()):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(ValueError, "INVALID_OBSERVATIONS"):
                    inspect(*rows)

    def test_untrusted_metadata_rejected(self):
        for kwargs in ({"asset_type": "CRYPTO"}, {"asset_type": []},
                       {"price_basis": "UNKNOWN"}, {"price_basis": []},
                       {"source": ""}, {"source_revision": "../bad"},
                       {"as_of": datetime(2026, 11, 4)},
                       {"source": "fetched-from-network\ninjected"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "INVALID_SNAPSHOT_METADATA"):
                    inspect(*self.bars, **kwargs)

    def test_deliberate_price_basis_is_part_of_receipt(self):
        raw = inspect(*self.bars)
        adjusted = inspect(*self.bars, price_basis="SPLIT_ADJUSTED")
        self.assertNotEqual(raw.sha256, adjusted.sha256)

    def test_metadata_is_in_digest(self):
        base = inspect(*self.bars).sha256
        changed = inspect(*self.bars, source_revision="new_batch").sha256
        self.assertNotEqual(base, changed)

    def test_snapshot_does_not_approve_or_submit_orders(self):
        receipt = inspect(*self.bars)
        self.assertFalse(hasattr(receipt, "place_order"))
        self.assertFalse(hasattr(receipt, "trade_eligible"))


if __name__ == "__main__":
    unittest.main()
