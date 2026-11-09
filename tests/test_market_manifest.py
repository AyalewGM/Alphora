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

    def test_saturday_and_sunday_sessions_are_not_trading_days(self):
        m = fixture()
        # A self-attested calendar could otherwise claim plausible 09:30-
        # 16:00 bars on weekends, silently changing replay/benchmark dates.
        # Keep every other point-in-time condition valid to isolate the gate.
        for weekend in (date(2025, 11, 22), date(2025, 11, 23)):
            with self.subTest(day=weekend):
                known_on = datetime(2025, 11, 21, 15, tzinfo=timezone.utc)
                candidate = replace(
                    m,
                    membership=replace(m.membership, day=date(2025, 11, 21),
                                       available_at=known_on),
                    calendar_available_at=known_on,
                    sessions=(session(weekend), m.sessions[-1]),
                    bar_days=(weekend, m.bar_days[-1]),
                )
                with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SESSION"):
                    check(candidate)
        # A declared normal weekday plus an explicit early close still passes.
        self.assertEqual(len(check(m).manifest.sessions), 2)

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

    def test_complete_daily_artifact_cannot_arrive_before_final_close(self):
        m = fixture()
        # Both a source version and a claimed delivery can predate the last
        # day's completed OHLCV bar while still preceding the as_of cutoff.
        # Such a receipt is chronologically impossible for a complete file.
        early_delivery = m.sessions[-1].closes_at - timedelta(minutes=1)
        candidate = replace(m, version_available_at=m.sessions[0].opens_at,
                            delivered_at=early_delivery)
        with self.assertRaisesRegex(ValueError, "PREMATURE_ARTIFACT_DELIVERY"):
            check(candidate)
        # An exact final close is the earliest chronologically possible
        # delivery, but does not authenticate any vendor timing assertion.
        on_close = replace(candidate,
                           version_available_at=m.sessions[-1].closes_at,
                           delivered_at=m.sessions[-1].closes_at)
        self.assertEqual(check(on_close).manifest.delivered_at,
                         m.sessions[-1].closes_at)

    def test_completed_artifact_version_cannot_precede_last_session_close(self):
        m = fixture()
        final_close = m.sessions[-1].closes_at
        # A version falsely labeled as available before the final close is
        # lookahead, even if the file was delivered after the final close.
        early_version = replace(m, version_available_at=final_close - timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "PREMATURE_ARTIFACT_VERSION"):
            check(early_version)
        # UTC and US Eastern representations of the same close are accepted.
        at_close = replace(m, version_available_at=final_close.astimezone(timezone.utc))
        self.assertEqual(check(at_close).manifest.version_available_at,
                         final_close.astimezone(timezone.utc))

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

    def test_active_security_cannot_end_its_last_symbol_without_delisting(self):
        m = fixture()
        # Existing bars alone can look complete even when the security-master
        # quietly stops reporting an active asset after the final supplied bar.
        last_day = m.bar_days[-1]
        incomplete = replace(m.instrument, symbols=(
            m.instrument.symbols[0],
            replace(m.instrument.symbols[1], end=last_day),
        ))
        with self.assertRaisesRegex(ValueError, "INVALID_SYMBOL_HISTORY"):
            check(replace(m, instrument=incomplete))
        # A genuinely delisted instrument has an explicit terminal date and
        # matching last symbol period, so it remains representable.
        settled = replace(incomplete, delisted_on=last_day)
        self.assertEqual(
            check(replace(m, instrument=settled)).manifest.instrument.delisted_on,
            last_day,
        )

    def test_future_dated_symbol_or_delisting_cannot_leak_into_snapshot(self):
        m = fixture()
        original = m.instrument.symbols
        after_cutoff = date(2026, 1, 2)
        before_future_change = date(2026, 1, 1)
        # Both historical bars are covered by NEW. Without a cutoff guard,
        # an otherwise-valid manifest could disclose a future ticker rename.
        future_symbol_end = replace(m.instrument, symbols=(
            original[0], SymbolPeriod("NEW", date(2025, 1, 2), before_future_change),
            SymbolPeriod("FUT", after_cutoff)))
        future_symbol_start = replace(m.instrument, symbols=(
            original[0], SymbolPeriod("NEW", date(2025, 1, 2), date(2025, 11, 29)),
            SymbolPeriod("FUT", after_cutoff)))
        future_delisting = replace(m.instrument, delisted_on=before_future_change,
                                  symbols=(original[0],
                                           SymbolPeriod("NEW", date(2025, 1, 2),
                                                        before_future_change)))
        for asset in (future_symbol_end, future_symbol_start, future_delisting):
            with self.subTest(asset=asset), self.assertRaisesRegex(
                    ValueError, "FUTURE_INSTRUMENT_EVENT"):
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

    def test_long_research_ticker_is_not_paper_ledger_compatible(self):
        m = fixture()
        plain = check(m)
        self.assertEqual(plain.paper_symbol, "NEW")
        self.assertEqual(plain.paper_symbol_reason,
                         "PAPER_SYMBOL_REPRESENTABLE_ONLY")
        for name in ("ABCDEFGHIJK", "ABCDEFGHIJKLMNO"):
            with self.subTest(symbol=name):
                instrument = replace(m.instrument, symbols=(
                    m.instrument.symbols[0],
                    replace(m.instrument.symbols[1], symbol=name)))
                new_m = replace(m, instrument=instrument)
                receipt = check(new_m)
                self.assertEqual(receipt.manifest.instrument.symbols[-1].symbol, name)
                self.assertIsNone(receipt.paper_symbol)
                self.assertEqual(receipt.paper_symbol_reason,
                                 "PAPER_SYMBOL_UNSUPPORTED_FORMAT")
                self.assertNotEqual(receipt.sha256, plain.sha256)
                self.assertEqual(check(
                    new_m, expected_manifest_sha256=receipt.sha256), receipt)
        # Longer than the manifest's 15-character security-master bound
        # is invalid research evidence; it is not normalized or truncated.
        bad = replace(m.instrument, symbols=(m.instrument.symbols[0],
                       replace(m.instrument.symbols[1],
                               symbol="ABCDEFGHIJKLMNOP")))
        with self.assertRaisesRegex(ValueError, "INVALID_SYMBOL_HISTORY"):
            check(replace(m, instrument=bad))

    def test_exclusion_and_delisting_do_not_emit_paper_symbol(self):
        m = fixture()
        excluded = replace(m, membership=replace(m.membership,
                           included=False, exclusion_reason="not_in_universe"))
        excluded_receipt = check(excluded)
        self.assertIsNone(excluded_receipt.paper_symbol)
        self.assertEqual(excluded_receipt.paper_symbol_reason,
                         "PAPER_UNIVERSE_EXCLUDED")
        last_day = m.bar_days[-1]
        delisted = replace(m.instrument, delisted_on=last_day,
                    symbols=(m.instrument.symbols[0],
                             replace(m.instrument.symbols[1], end=last_day)))
        receipt = check(replace(m, instrument=delisted))
        self.assertEqual(receipt.manifest.instrument.delisted_on, last_day)
        self.assertIsNone(receipt.paper_symbol)
        self.assertEqual(receipt.paper_symbol_reason,
                         "PAPER_INSTRUMENT_DELISTED")

    def test_oversized_session_manifest_rejected_before_iteration(self):
        from alphora.research.market_manifest import MAX_MANIFEST_SESSIONS
        m = fixture()
        repeated_session = (m.sessions[0],) * (MAX_MANIFEST_SESSIONS + 1)
        repeated_days = (m.bar_days[0],) * (MAX_MANIFEST_SESSIONS + 1)
        with self.assertRaisesRegex(ValueError, "MANIFEST_TOO_LARGE"):
            check(replace(m, sessions=repeated_session, bar_days=repeated_days))
        # A huge declared calendar with fewer bar days still cannot sneak
        # through as a validated, full-coverage market history.
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_SESSION_COVERAGE"):
            check(replace(m, sessions=repeated_session))

    def test_oversized_ticker_history_rejected_before_period_traversal(self):
        from alphora.research.market_manifest import MAX_SYMBOL_PERIODS
        m = fixture()
        symbols = (m.instrument.symbols[0],) * (MAX_SYMBOL_PERIODS + 1)
        with self.assertRaisesRegex(ValueError, "MANIFEST_TOO_LARGE"):
            check(replace(m, instrument=replace(m.instrument, symbols=symbols)))
        # Ordinary bounded, point-in-time symbol histories still validate.
        self.assertEqual(check(m).paper_symbol, "NEW")

    def test_receipt_does_not_offer_trading_capabilities(self):
        receipt = check(fixture())
        for name in ("place_order", "trade_eligible", "load_broker", "buy"):
            self.assertFalse(hasattr(receipt, name))


if __name__ == "__main__":
    unittest.main()
