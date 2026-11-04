"""Synthetic-only tests for offline, caller-attested provenance. No networking."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from alphora.research.market_manifest import (
    ExchangeSession, Instrument, MarketManifest, SymbolPeriod,
    UniverseMembership, inspect_market_manifest,
)

ET = ZoneInfo("America/New_York")
BAR_HASH = "a" * 64
CAL_HASH = "b" * 64


def session(day, *, early=False):
    return ExchangeSession(day,
                           datetime.combine(day, datetime.min.time().replace(hour=9, minute=30), ET),
                           datetime.combine(day, datetime.min.time().replace(hour=13 if early else 16), ET),
                           "EARLY_CLOSE" if early else "REGULAR")


def fixture():
    d1, d2 = date(2025, 11, 26), date(2025, 11, 28)  # holiday on 27th, early close on 28th
    return MarketManifest(
        1, Instrument("perm-asset-123", "STOCK", "XNYS", date(2020, 1, 1), None,
                      (SymbolPeriod("OLD", date(2020, 1, 1), date(2025, 1, 1)),
                       SymbolPeriod("NEW", date(2025, 1, 2)),)),
        UniverseMembership(date(2025, 11, 25), True, None,
                           datetime(2025, 11, 25, 15, tzinfo=timezone.utc)),
        "synthetic_vendor", "daily_raw", "revision_1", "local_research_test", True,
        datetime(2025, 11, 29, tzinfo=timezone.utc),
        datetime(2025, 11, 28, 21, tzinfo=timezone.utc),
        BAR_HASH, "synthetic_calendar", "cal-v1", CAL_HASH,
        datetime(2025, 11, 25, tzinfo=timezone.utc),
        (session(d1), session(d2, early=True)), (d1, d2),
        datetime(2025, 11, 30, tzinfo=timezone.utc),
    )


def check(m, **kwargs):
    return inspect_market_manifest(m, expected_artifact_sha256=BAR_HASH,
                                   expected_calendar_sha256=CAL_HASH, **kwargs)


class MarketManifestTests(unittest.TestCase):
    def test_synthetic_rename_and_early_close_receipt(self):
        m = fixture()
        r = check(m)
        self.assertEqual(r.manifest.instrument.asset_id, "perm-asset-123")
        self.assertEqual(len(r.sha256), 64)
        self.assertEqual(check(m, expected_manifest_sha256=r.sha256), r)
        self.assertEqual(check(replace(m, as_of=m.as_of.astimezone(ET))).sha256, r.sha256)

    def test_missing_reordered_and_duplicate_sessions(self):
        m = fixture()
        for v in (replace(m, bar_days=m.bar_days[:1]),
                  replace(m, bar_days=m.bar_days[::-1]),
                  replace(m, sessions=(m.sessions[0], m.sessions[0]))):
            with self.subTest(v=v), self.assertRaisesRegex(ValueError, "INCOMPLETE_SESSION_COVERAGE"):
                check(v)

    def test_early_close_must_be_explicitly_declared(self):
        m = fixture()
        wrong = replace(m.sessions[-1], kind="REGULAR")
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SESSION"):
            check(replace(m, sessions=(m.sessions[0], wrong)))

    def test_unknown_halt_not_inferred(self):
        m = fixture()
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SESSION"):
            check(replace(m, sessions=(m.sessions[0], replace(m.sessions[1], kind="HALT"))))

    def test_calendar_revision_after_first_open_is_hindsight(self):
        m = fixture()
        with self.assertRaisesRegex(ValueError, "POINT_IN_TIME_BOUNDARY"):
            check(replace(m, calendar_available_at=m.sessions[0].opens_at))

    def test_vendor_revision_arriving_after_cutoff_rejected(self):
        m = fixture()
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_REVISION"):
            check(replace(m, delivered_at=m.as_of + timedelta(seconds=1)))
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE_REVISION"):
            check(replace(m, version_available_at=m.delivered_at + timedelta(seconds=1)))

    def test_calendar_future_or_naive_rejected(self):
        m = fixture()
        for t in (m.as_of + timedelta(seconds=1), datetime(2025, 11, 25)):
            with self.subTest(t=t), self.assertRaisesRegex(ValueError, "UNAVAILABLE_REVISION"):
                check(replace(m, calendar_available_at=t))

    def test_external_pins_must_match(self):
        m = fixture()
        for kwargs in ({"expected_artifact_sha256": "c" * 64},
                       {"expected_calendar_sha256": "c" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "EXTERNAL_PIN_MISMATCH"):
                inspect_market_manifest(m, **{**dict(expected_artifact_sha256=BAR_HASH,
                                                    expected_calendar_sha256=CAL_HASH), **kwargs})
        with self.assertRaisesRegex(ValueError, "MANIFEST_PIN_MISMATCH"):
            check(m, expected_manifest_sha256="f" * 64)

    def test_unlicensed_or_missing_grant_rejected(self):
        m = fixture()
        for val in (False, None, 1):
            with self.subTest(val=val), self.assertRaisesRegex(ValueError, "LICENSE_OR_SOURCE_UNVERIFIED"):
                check(replace(m, authorized_for_local_research=val))

    def test_symbol_gap_duplicate_and_delist_rejected(self):
        m = fixture()
        a = m.instrument
        for asset in (
            replace(a, symbols=(SymbolPeriod("OLD", date(2020, 1, 1), date(2025, 11, 26)),)),
            replace(a, symbols=(SymbolPeriod("OLD", date(2020, 1, 1)), SymbolPeriod("NEW", date(2025, 1, 2)))),
            replace(a, delisted_on=date(2025, 11, 26)),
        ):
            with self.subTest(asset=asset), self.assertRaises(ValueError):
                check(replace(m, instrument=asset))

    def test_universe_exclusion_has_reason_and_changes_hash(self):
        m = fixture()
        excluded = replace(m, membership=replace(m.membership, included=False,
                                                  exclusion_reason="not_in_universe"))
        self.assertNotEqual(check(excluded).sha256, check(m).sha256)
        with self.assertRaisesRegex(ValueError, "INVALID_UNIVERSE_MEMBERSHIP"):
            check(replace(m, membership=replace(m.membership, included=False, exclusion_reason=None)))

    def test_universe_lookahead_rejected(self):
        m = fixture()
        with self.assertRaisesRegex(ValueError, "POINT_IN_TIME_BOUNDARY"):
            check(replace(m, membership=replace(m.membership,
                         available_at=m.sessions[0].opens_at)))

    def test_data_changes_in_digest(self):
        m = fixture()
        self.assertNotEqual(check(m).sha256, check(replace(m, revision="revision_2")).sha256)
        self.assertNotEqual(check(m).sha256,
                            check(replace(m, instrument=replace(m.instrument, asset_id="new-id"))).sha256)

    def test_receipt_does_not_offer_trading_capabilities(self):
        receipt = check(fixture())
        for name in ("place_order", "trade_eligible", "load_broker", "buy"):
            self.assertFalse(hasattr(receipt, name))


if __name__ == "__main__":
    unittest.main()
