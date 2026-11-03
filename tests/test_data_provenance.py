"""Synthetic offline provenance tests; no external feed, networking or trades."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
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
