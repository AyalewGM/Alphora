"""Synthetic offline tests only: no network, provider data, or broker calls."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from alphora.research.asset_universe import (
    AssetMembership, TickerPeriod, inspect_universe,
)

UTC = timezone.utc
AS_OF = datetime(2026, 11, 2, 16, 0, tzinfo=UTC)


def asset(**changes):
    original = AssetMembership(
        asset_id="US-123", venue_mic="XNYS", asset_class="STOCK",
        listing_date=date(2020, 1, 1), delisting_date=None,
        ticker_history=(TickerPeriod("OLD", date(2020, 1, 1),
                                     date(2024, 1, 1)),
                        TickerPeriod("NEW", date(2024, 1, 1))),
        membership_available_at=AS_OF - timedelta(minutes=1),
        included=True)
    return replace(original, **changes)


def inspect(records, **kw):
    return inspect_universe(records, as_of=AS_OF, source="synthetic",
                            revision="revision-1", license_claim="synthetic-only",
                            **kw)


class UniverseContractTests(unittest.TestCase):
    def test_stable_receipt_reorders_assets_not_history_or_identity(self):
        a = asset()
        b = replace(a, asset_id="US-456", venue_mic="XNAS",
                    ticker_history=(TickerPeriod("QQQ", date(2020, 1, 1)),))
        first = inspect((a, b))
        self.assertEqual(first.sha256, inspect((b, a)).sha256)
        self.assertEqual(first.assets, (a, b))
        self.assertEqual(first.sha256,
                         inspect((a, b), expected_sha256=first.sha256).sha256)
        with self.assertRaisesRegex(ValueError, "UNIVERSE_HASH_MISMATCH"):
            inspect((replace(a, asset_id="US-124"), b),
                    expected_sha256=first.sha256)

    def test_ticker_renames_keep_stable_asset_id_but_change_receipt(self):
        baseline = inspect((asset(),))
        old_alias = TickerPeriod("OLD", date(2020, 1, 1), date(2024, 1, 1))
        changed = asset(ticker_history=(old_alias,
                        TickerPeriod("ALT", date(2024, 1, 1))))
        self.assertNotEqual(inspect((changed,)).sha256, baseline.sha256)

    def test_future_membership_evidence_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVALID_ASSET_RECORD"):
            inspect((asset(membership_available_at=AS_OF + timedelta(seconds=1)),))

    def test_duplicate_asset_and_active_ticker_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVALID_ASSET_RECORD"):
            inspect((asset(), asset()))
        collision = replace(asset(), asset_id="US-456")
        with self.assertRaisesRegex(ValueError, "DUPLICATE_ACTIVE_TICKER"):
            inspect((asset(), collision))

    def test_invalid_or_incomplete_ticker_history_rejected(self):
        bad_histories = (
            (TickerPeriod("OLD", date(2020, 1, 2)),),
            (TickerPeriod("OLD", date(2020, 1, 1), date(2024, 1, 2)),
             TickerPeriod("NEW", date(2024, 1, 1))),
            (TickerPeriod("OLD", date(2020, 1, 1)),
             TickerPeriod("NEW", date(2024, 1, 1))),
            (TickerPeriod("OLD", date(2020, 1, 1), date(2024, 1, 1)),
             TickerPeriod("NEW", date(2024, 1, 2))),
        )
        for history in bad_histories:
            with self.subTest(history=history), self.assertRaisesRegex(
                    ValueError, "INVALID_TICKER_HISTORY"):
                inspect((asset(ticker_history=history),))

    def test_exclusion_reason_and_delisting_invariants(self):
        early = replace(asset(), listing_date=date(2026, 11, 3),
                        ticker_history=(TickerPeriod("NEW", date(2026, 11, 3)),),
                        included=False, exclusion_reason="NOT_YET_LISTED")
        self.assertFalse(inspect((early,)).assets[0].included)
        historical = replace(
            asset(), delisting_date=date(2026, 10, 30),
            ticker_history=(
                TickerPeriod("OLD", date(2020, 1, 1), date(2024, 1, 1)),
                TickerPeriod("NEW", date(2024, 1, 1), date(2026, 10, 31))),
            included=False, exclusion_reason="DELISTED")
        self.assertFalse(inspect((historical,)).assets[0].included)
        # An open-ended ticker history contradicts a completed delisting.
        with self.assertRaisesRegex(ValueError, "INVALID_DELISTED_TICKER_HISTORY"):
            inspect((asset(delisting_date=date(2026, 10, 30),
                           included=False, exclusion_reason="DELISTED"),))
        for candidate in (
            asset(delisting_date=date(2026, 10, 30)),
            replace(early, included=True, exclusion_reason=None),
            asset(included=False, exclusion_reason=None),
            asset(included=False, exclusion_reason="NOT_YET_LISTED"),
            asset(included=False, exclusion_reason="DELISTED"),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                inspect((candidate,))

    def test_aware_timestamp_and_explicit_license_claim_required(self):
        with self.assertRaisesRegex(ValueError, "INVALID_UNIVERSE_METADATA"):
            inspect_universe((asset(),), as_of=datetime(2026, 11, 2),
                             source="synthetic", revision="v1",
                             license_claim="synthetic-only")
        with self.assertRaisesRegex(ValueError, "INVALID_UNIVERSE_METADATA"):
            inspect((asset(),), expected_sha256="bad")
        with self.assertRaisesRegex(ValueError, "INVALID_UNIVERSE_METADATA"):
            inspect_universe((asset(),), as_of=AS_OF,
                             source="synthetic", revision="v1",
                             license_claim="")

    def test_utc_and_eastern_as_of_representations_match(self):
        utc = inspect((asset(),)).sha256
        et = inspect_universe((asset(),),
                              as_of=AS_OF.astimezone(ZoneInfo("America/New_York")),
                              source="synthetic", revision="revision-1",
                              license_claim="synthetic-only").sha256
        self.assertEqual(utc, et)

    def test_strict_membership_bool_and_symbol_format(self):
        with self.assertRaisesRegex(ValueError, "INVALID_ASSET_RECORD"):
            inspect((asset(included=1),))
        with self.assertRaisesRegex(ValueError, "INVALID_TICKER_HISTORY"):
            inspect((asset(ticker_history=(TickerPeriod("bad;SYM",
                                  date(2020, 1, 1)),)),))


if __name__ == "__main__":
    unittest.main()
