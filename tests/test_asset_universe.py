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

    def test_future_ticker_alias_and_delisting_rejected(self):
        future_alias = asset(ticker_history=(
            TickerPeriod("OLD", date(2020, 1, 1), date(2024, 1, 1)),
            TickerPeriod("NEW", date(2024, 1, 1), date(2027, 1, 1)),
            TickerPeriod("NEXT", date(2027, 1, 1))))
        with self.assertRaisesRegex(ValueError, "FUTURE_TICKER_HISTORY"):
            inspect((future_alias,))
        with self.assertRaisesRegex(ValueError, "FUTURE_DELISTING_EVENT"):
            inspect((asset(delisting_date=date(2027, 1, 1)),))

    def test_future_membership_evidence_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVALID_ASSET_RECORD"):
            inspect((asset(membership_available_at=AS_OF + timedelta(seconds=1)),))

    def test_dst_fold_rejects_future_membership_by_instant(self):
        et = ZoneInfo("America/New_York")
        early = datetime(2026, 11, 1, 1, 45, tzinfo=et, fold=0)
        late = datetime(2026, 11, 1, 1, 15, tzinfo=et, fold=1)
        options = dict(source="synthetic", revision="v1", license_claim="synthetic-only")
        for future in (late, early.replace(fold=1)):
            with self.subTest(future=future), self.assertRaisesRegex(
                    ValueError, "INVALID_ASSET_RECORD"):
                inspect_universe((asset(membership_available_at=future),),
                                 as_of=early, **options)
        valid = asset(membership_available_at=early)
        receipt = inspect_universe((valid,), as_of=late, **options)
        self.assertEqual(receipt.sha256, inspect_universe(
            (replace(valid, membership_available_at=early.astimezone(UTC)),),
            as_of=late.astimezone(UTC), **options).sha256)

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

    def test_delisting_day_is_included_through_last_listing_day(self):
        # Synthetic as-of is the last listed trading date. A closed interval
        # ends at the *next* calendar day (exclusive) by construction; this
        # must not be mistaken for future ticker evidence.
        last_day_as_of = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)
        terminal = replace(
            asset(), delisting_date=date(2026, 10, 30),
            ticker_history=(
                TickerPeriod("OLD", date(2020, 1, 1), date(2024, 1, 1)),
                TickerPeriod("NEW", date(2024, 1, 1), date(2026, 10, 31)),
            ),
            membership_available_at=last_day_as_of - timedelta(minutes=1),
        )
        receipt = inspect_universe(
            (terminal,), as_of=last_day_as_of, source="synthetic",
            revision="revision-1", license_claim="synthetic-only",
        )
        self.assertEqual(receipt.assets, (terminal,))
        # Any extension beyond the exclusive close still represents a
        # future-dated ticker event and must be rejected.
        future_close = replace(terminal, ticker_history=(
            terminal.ticker_history[0],
            TickerPeriod("NEW", date(2024, 1, 1), date(2026, 11, 1)),
        ))
        with self.assertRaisesRegex(ValueError, "FUTURE_TICKER_HISTORY"):
            inspect_universe(
                (future_close,), as_of=last_day_as_of, source="synthetic",
                revision="revision-1", license_claim="synthetic-only",
            )

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
