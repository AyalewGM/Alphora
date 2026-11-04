"""Offline-only synthetic replay contract; never touches market or broker APIs."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.research.backtest import Bar, backtest
from alphora.research.data_provenance import ObservedDailyBar, inspect_daily_snapshot
from alphora.research.replay import (
    replay_candidate_snapshot, compare_candidate_snapshots,
    session_manifest_sha256,
)

ET = ZoneInfo("America/New_York")


def candidate(symbol="SPY", *, asset_type="ETF", basis="RAW_UNADJUSTED",
              day_shift=0, as_of=None):
    # Deliberately synthetic weekday samples, not a vetted exchange calendar.
    days = [date(2026, 9, 1) + timedelta(days=i + day_shift)
            for i in range(70)]
    days = [d for d in days if d.weekday() < 5][:35]
    observations = []
    for i, day in enumerate(days):
        opening = D(100 + i)
        bar = Bar(day, opening, opening + D(2),
                  opening - D(2), opening + D(1), 1000000)
        close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)
        observations.append(ObservedDailyBar(
            bar, close + timedelta(minutes=5), close + timedelta(minutes=10)))
    cut = as_of if as_of is not None else (
        observations[-1].version_available_at + timedelta(hours=2))
    return inspect_daily_snapshot(
        symbol, observations, asset_type=asset_type,
        source="synthetic_local", source_revision="offline_fixture_v1",
        price_basis=basis, as_of=cut
    )


class OfflineSnapshotReplayTests(unittest.TestCase):
    def setUp(self):
        self.spy = candidate()

    @staticmethod
    def _zero_volume_snapshot(original, index=1):
        # The generic bar validator allows zero volume (halts do occur), and
        # rebuilding/re-pinning produces an otherwise valid dataset receipt.
        records = list(original.observations)
        records[index] = replace(records[index], bar=replace(
            records[index].bar, volume=0))
        return inspect_daily_snapshot(
            original.symbol, records, asset_type=original.asset_type,
            source=original.source, source_revision=original.source_revision,
            price_basis=original.price_basis, as_of=original.as_of,
        )

    def test_pinned_replay_rejects_zero_volume_sessions(self):
        stopped = self._zero_volume_snapshot(self.spy, index=1)
        self.assertNotEqual(stopped.sha256, self.spy.sha256)
        self.assertEqual(stopped.bars[1].volume, 0)
        with self.assertRaisesRegex(
                ValueError, "ZERO_VOLUME_RESEARCH_REPLAY_UNSUPPORTED"):
            replay_candidate_snapshot(
                stopped, "buy_hold", expected_sha256=stopped.sha256)
        # Reject even a non-entry session; the offline engine has no halt
        # or reopening-auction model that could safely interpret such a bar.
        later_halt = self._zero_volume_snapshot(self.spy, index=15)
        with self.assertRaisesRegex(
                ValueError, "ZERO_VOLUME_RESEARCH_REPLAY_UNSUPPORTED"):
            replay_candidate_snapshot(
                later_halt, "trend", expected_sha256=later_halt.sha256)

    def test_spy_comparison_rejects_zero_volume_in_either_leg(self):
        aapl = candidate("AAPL", asset_type="STOCK")
        for bad_candidate, bad_spy in (
            (self._zero_volume_snapshot(aapl), self.spy),
            (aapl, self._zero_volume_snapshot(self.spy)),
        ):
            with self.subTest(zero_volume_symbol=bad_candidate.symbol
                              if bad_candidate.bars[1].volume == 0 else "SPY"):
                with self.assertRaisesRegex(
                        ValueError, "ZERO_VOLUME_RESEARCH_REPLAY_UNSUPPORTED"):
                    compare_candidate_snapshots(
                        bad_candidate, bad_spy, "buy_hold",
                        expected_strategy_sha256=bad_candidate.sha256,
                        expected_spy_sha256=bad_spy.sha256)

    def test_replays_only_pinned_snapshot_with_approved_research_allocation(self):
        output = replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256,
            cost_bps=D("5"))
        self.assertEqual(output.candidate_snapshot_sha256, self.spy.sha256)
        self.assertEqual(output.source, "synthetic_local")
        self.assertEqual(output.result, backtest(
            self.spy.bars, "buy_hold", initial=D("100000"),
            cost_bps=D("5"), max_allocation=D("0.10")))
        self.assertEqual(len(output.result.equity_curve), 35)
        self.assertFalse(hasattr(output, "submit_order"))

    def test_missing_or_wrong_external_hash_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "EXPECTED_DIGEST_REQUIRED"):
            replay_candidate_snapshot(self.spy, "buy_hold",
                                      expected_sha256=None)
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_HASH_MISMATCH"):
            replay_candidate_snapshot(self.spy, "buy_hold",
                                      expected_sha256="0" * 64)

    def test_mutated_bar_content_or_reported_digest_fails_closed(self):
        first = self.spy.observations[0]
        altered = replace(first, bar=replace(first.bar, close=D("102")))
        changed = replace(self.spy,
                          observations=(altered,) + self.spy.observations[1:])
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_HASH_MISMATCH"):
            replay_candidate_snapshot(
                changed, "buy_hold", expected_sha256=self.spy.sha256)
        changed_digest = replace(self.spy, sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_WAS_MODIFIED"):
            replay_candidate_snapshot(
                changed_digest, "buy_hold", expected_sha256=self.spy.sha256)

    def test_replayed_snapshot_still_enforces_version_availability(self):
        first = self.spy.observations[0]
        next_day = self.spy.observations[1].bar.day
        late = replace(first, version_available_at=datetime(
            next_day.year, next_day.month, next_day.day, 9, 31, tzinfo=ET))
        changed = replace(self.spy, observations=(late,) + self.spy.observations[1:])
        with self.assertRaisesRegex(ValueError, "NEXT_OPEN_LOOKAHEAD"):
            replay_candidate_snapshot(
                changed, "buy_hold", expected_sha256=self.spy.sha256)

    def test_split_adjusted_snapshot_rejected_until_accounting_is_supported(self):
        adjusted = candidate(basis="SPLIT_ADJUSTED")
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_PRICE_BASIS"):
            replay_candidate_snapshot(
                adjusted, "buy_hold", expected_sha256=adjusted.sha256)

    def test_invalid_strategy_lookback_and_cost_rejected(self):
        for name, period in (("magic", 20), ("trend", True),
                             ("trend", 1), ("trend", 2.0)):
            with self.subTest(strategy=name, lookback=period):
                with self.assertRaisesRegex(ValueError, "INVALID_RESEARCH_PARAMETERS"):
                    replay_candidate_snapshot(
                        self.spy, name, expected_sha256=self.spy.sha256,
                        lookback=period)
        with self.assertRaisesRegex(ValueError, "INVALID_PARAMETERS"):
            replay_candidate_snapshot(
                self.spy, "buy_hold", expected_sha256=self.spy.sha256,
                cost_bps=D("-1"))

    def test_pinned_spy_comparison_is_aligned_and_exposure_caveat_visible(self):
        aapl = candidate("AAPL", asset_type="STOCK")
        result = compare_candidate_snapshots(
            aapl, self.spy, "buy_hold",
            expected_strategy_sha256=aapl.sha256,
            expected_spy_sha256=self.spy.sha256, cost_bps=D(0))
        self.assertEqual(result.strategy_snapshot_sha256, aapl.sha256)
        self.assertEqual(result.spy_snapshot_sha256, self.spy.sha256)
        self.assertEqual(result.comparison.excess_vs_capped_buy_hold, D(0))
        self.assertGreater(result.comparison.fully_invested_spy_proxy.total_return,
                           result.comparison.constrained_spy_proxy.total_return)

    def test_spy_identity_asof_and_calendar_mismatch_rejected(self):
        aapl = candidate("AAPL", asset_type="STOCK")
        with self.assertRaisesRegex(ValueError, "SPY_BENCHMARK_REQUIRED"):
            compare_candidate_snapshots(
                aapl, aapl, "buy_hold",
                expected_strategy_sha256=aapl.sha256,
                expected_spy_sha256=aapl.sha256)
        other_cut = self.spy.as_of + timedelta(minutes=1)
        changed_asof = candidate(as_of=other_cut)
        with self.assertRaisesRegex(ValueError, "BENCHMARK_AS_OF_MISMATCH"):
            compare_candidate_snapshots(
                aapl, changed_asof, "buy_hold",
                expected_strategy_sha256=aapl.sha256,
                expected_spy_sha256=changed_asof.sha256)
        shifted = candidate(day_shift=-1, as_of=self.spy.as_of)
        with self.assertRaisesRegex(ValueError, "BENCHMARK_DATE_MISMATCH"):
            compare_candidate_snapshots(
                shifted, self.spy, "buy_hold",
                expected_strategy_sha256=shifted.sha256,
                expected_spy_sha256=self.spy.sha256)

    def test_external_session_manifest_detects_missing_bars_after_repinning(self):
        # The native receipt validator cannot detect a missing market session:
        # its next-open check uses the next *recorded* bar. A separately
        # supplied expected-session manifest can reject that same omission.
        days = tuple(observation.bar.day for observation in self.spy.observations)
        missing = inspect_daily_snapshot(
            self.spy.symbol,
            self.spy.observations[:8] + self.spy.observations[9:],
            asset_type=self.spy.asset_type,
            source=self.spy.source, source_revision=self.spy.source_revision,
            price_basis=self.spy.price_basis, as_of=self.spy.as_of,
        )
        result = replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256,
            expected_session_dates=days,
        )
        self.assertTrue(result.session_manifest_matched)
        self.assertFalse(replay_candidate_snapshot(
            missing, "buy_hold", expected_sha256=missing.sha256,
        ).session_manifest_matched)
        with self.assertRaisesRegex(ValueError, "SESSION_MANIFEST_MISMATCH"):
            replay_candidate_snapshot(
                missing, "buy_hold", expected_sha256=missing.sha256,
                expected_session_dates=days,
            )

    def test_session_manifest_must_have_exact_dates_in_order(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        for invalid in ([], days[::-1], days[:1] + days[:1] + days[2:],
                        (datetime(2026, 9, 1),) + days[1:],
                        ("2026-09-01",) + days[1:]):
            with self.subTest(manifest=repr(invalid)[:100]):
                with self.assertRaisesRegex(ValueError, "INVALID_SESSION_MANIFEST"):
                    replay_candidate_snapshot(
                        self.spy, "buy_hold",
                        expected_sha256=self.spy.sha256,
                        expected_session_dates=invalid,
                    )

    def test_spy_comparison_requires_both_series_to_match_session_manifest(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        aapl = candidate("AAPL", asset_type="STOCK")
        result = compare_candidate_snapshots(
            aapl, self.spy, "trend",
            expected_strategy_sha256=aapl.sha256,
            expected_spy_sha256=self.spy.sha256,
            expected_session_dates=days, cost_bps=D("10"), lookback=5,
        )
        self.assertTrue(result.session_manifest_matched)
        self.assertEqual(result.cost_bps, D("10"))
        self.assertEqual(result.lookback, 5)
        self.assertFalse(compare_candidate_snapshots(
            aapl, self.spy, "buy_hold",
            expected_strategy_sha256=aapl.sha256,
            expected_spy_sha256=self.spy.sha256,
        ).session_manifest_matched)
        with self.assertRaisesRegex(ValueError, "SESSION_MANIFEST_MISMATCH"):
            compare_candidate_snapshots(
                aapl, self.spy, "buy_hold",
                expected_strategy_sha256=aapl.sha256,
                expected_spy_sha256=self.spy.sha256,
                expected_session_dates=days[:-1],
            )

    def test_replay_records_cost_and_lookback_assumptions(self):
        result = replay_candidate_snapshot(
            self.spy, "trend", expected_sha256=self.spy.sha256,
            lookback=5, cost_bps=D("12.5"),
        )
        self.assertEqual(result.cost_bps, D("12.5"))
        self.assertEqual(result.lookback, 5)
        self.assertFalse(result.session_manifest_matched)

    def test_benchmark_cannot_bypass_snapshot_pinning(self):
        aapl = candidate("AAPL", asset_type="STOCK")
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_HASH_MISMATCH"):
            compare_candidate_snapshots(
                aapl, self.spy, "trend",
                expected_strategy_sha256=aapl.sha256,
                expected_spy_sha256="0" * 64)


    def test_external_session_digest_is_recorded_without_authentication_claim(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        digest = session_manifest_sha256(days)
        self.assertEqual(digest, session_manifest_sha256(list(days)))
        replay = replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256,
            expected_session_dates=days, expected_session_manifest_sha256=digest)
        self.assertTrue(replay.session_manifest_matched)
        self.assertTrue(replay.session_manifest_digest_matched)
        unpinned = replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256,
            expected_session_dates=days)
        self.assertTrue(unpinned.session_manifest_matched)
        self.assertFalse(unpinned.session_manifest_digest_matched)
        self.assertFalse(replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256
        ).session_manifest_digest_matched)

    def test_manifest_change_cannot_pass_using_old_external_digest(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        pinned = session_manifest_sha256(days)
        omitted = days[:8] + days[9:]
        # A separately supplied pin detects even a syntactically valid
        # rewritten manifest before checking its equality with the bars.
        with self.assertRaisesRegex(ValueError, "SESSION_MANIFEST_HASH_MISMATCH"):
            replay_candidate_snapshot(
                self.spy, "buy_hold", expected_sha256=self.spy.sha256,
                expected_session_dates=omitted,
                expected_session_manifest_sha256=pinned)
        with self.assertRaisesRegex(ValueError, "SESSION_MANIFEST_MISMATCH"):
            replay_candidate_snapshot(
                self.spy, "buy_hold", expected_sha256=self.spy.sha256,
                expected_session_dates=omitted,
                expected_session_manifest_sha256=session_manifest_sha256(omitted))

    def test_missing_or_malformed_session_digest_fails_closed(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        with self.assertRaisesRegex(ValueError, "SESSION_MANIFEST_REQUIRED"):
            replay_candidate_snapshot(
                self.spy, "buy_hold", expected_sha256=self.spy.sha256,
                expected_session_manifest_sha256="0" * 64)
        for bad in ("NOT_A_SHA256", "0" * 63, "A" * 64, 1, [], True):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                        ValueError, "INVALID_SESSION_MANIFEST_SHA256"):
                    replay_candidate_snapshot(
                        self.spy, "buy_hold", expected_sha256=self.spy.sha256,
                        expected_session_dates=days,
                        expected_session_manifest_sha256=bad)

    def test_spy_comparison_requires_both_datasets_and_pin(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        digest = session_manifest_sha256(days)
        aapl = candidate("AAPL", asset_type="STOCK")
        verified = compare_candidate_snapshots(
            aapl, self.spy, "trend",
            expected_strategy_sha256=aapl.sha256,
            expected_spy_sha256=self.spy.sha256,
            expected_session_dates=days,
            expected_session_manifest_sha256=digest)
        self.assertTrue(verified.session_manifest_matched)
        self.assertTrue(verified.session_manifest_digest_matched)
        with self.assertRaisesRegex(ValueError, "SESSION_MANIFEST_HASH_MISMATCH"):
            compare_candidate_snapshots(
                aapl, self.spy, "trend",
                expected_strategy_sha256=aapl.sha256,
                expected_spy_sha256=self.spy.sha256,
                expected_session_dates=days,
                expected_session_manifest_sha256="0" * 64)

    def test_replay_without_manifest_has_no_audit_receipt(self):
        result = replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256)
        self.assertFalse(result.session_manifest_matched)
        self.assertFalse(result.session_manifest_digest_matched)
        self.assertIsNone(result.session_manifest_sha256)
        self.assertIsNone(result.session_manifest_session_count)

    def test_unpinned_session_receipt_is_reproducible_not_authenticated(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        result = replay_candidate_snapshot(
            self.spy, "buy_hold", expected_sha256=self.spy.sha256,
            expected_session_dates=days)
        self.assertTrue(result.session_manifest_matched)
        self.assertFalse(result.session_manifest_digest_matched)
        self.assertEqual(result.session_manifest_sha256,
                         session_manifest_sha256(days))
        self.assertEqual(result.session_manifest_session_count, len(days))

    def test_pinned_session_receipt_is_recorded_exactly(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        pin = session_manifest_sha256(days)
        result = replay_candidate_snapshot(
            self.spy, "trend", expected_sha256=self.spy.sha256,
            expected_session_dates=days,
            expected_session_manifest_sha256=pin)
        self.assertTrue(result.session_manifest_digest_matched)
        self.assertEqual(result.session_manifest_sha256, pin)
        self.assertEqual(result.session_manifest_session_count,
                         len(self.spy.bars))

    def test_spy_comparison_audit_receipt_matches_both_inputs(self):
        days = tuple(item.bar.day for item in self.spy.observations)
        pin = session_manifest_sha256(days)
        aapl = candidate("AAPL", asset_type="STOCK")
        result = compare_candidate_snapshots(
            aapl, self.spy, "trend",
            expected_strategy_sha256=aapl.sha256,
            expected_spy_sha256=self.spy.sha256,
            expected_session_dates=days,
            expected_session_manifest_sha256=pin)
        self.assertEqual(result.session_manifest_sha256, pin)
        self.assertEqual(result.session_manifest_session_count, len(days))
        self.assertTrue(result.session_manifest_digest_matched)


    def test_spy_replay_exposes_equal_session_risk_metrics(self):
        aapl = candidate("AAPL", asset_type="STOCK")
        compared = compare_candidate_snapshots(
            aapl, self.spy, "trend",
            expected_strategy_sha256=aapl.sha256,
            expected_spy_sha256=self.spy.sha256, cost_bps=D("5"))
        metrics = compared.comparison
        self.assertEqual(metrics.evaluation_start, 20)
        self.assertEqual(metrics.aligned_strategy.sessions, 15)
        self.assertEqual(metrics.aligned_capped_spy.sessions, 15)
        self.assertEqual(metrics.aligned_fully_invested_spy.sessions, 15)
        self.assertEqual(metrics.excess_vs_capped_buy_hold,
                         metrics.aligned_strategy.total_return -
                         metrics.aligned_capped_spy.total_return)

    def test_research_fingerprint_is_stable_under_repeated_equivalent_inputs(self):
        from datetime import timezone
        first = replay_candidate_snapshot(
            self.spy, "trend", expected_sha256=self.spy.sha256,
            cost_bps=D("5"), lookback=5)
        second = replay_candidate_snapshot(
            self.spy, "trend", expected_sha256=self.spy.sha256,
            cost_bps=D("5.000"), lookback=5)
        # Equivalent timezone representations and decimal scaling are not
        # different research trials.
        normalized = replace(self.spy,
                             as_of=self.spy.as_of.astimezone(timezone.utc))
        third = replay_candidate_snapshot(
            normalized, "trend", expected_sha256=self.spy.sha256,
            cost_bps=D("5.0"), lookback=5)
        self.assertRegex(first.input_fingerprint_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(first.input_fingerprint_sha256,
                         second.input_fingerprint_sha256)
        self.assertEqual(second.input_fingerprint_sha256,
                         third.input_fingerprint_sha256)

    def test_research_fingerprint_changes_with_strategy_cost_and_lookback(self):
        base = replay_candidate_snapshot(
            self.spy, "trend", expected_sha256=self.spy.sha256,
            cost_bps=D("5"), lookback=5)
        variants = [
            ("momentum", D("5"), 5),
            ("trend", D("5.01"), 5),
            ("trend", D("5"), 6),
        ]
        for strategy, cost, lookback in variants:
            with self.subTest(strategy=strategy, cost=cost, lookback=lookback):
                alternate = replay_candidate_snapshot(
                    self.spy, strategy, expected_sha256=self.spy.sha256,
                    cost_bps=cost, lookback=lookback)
                self.assertNotEqual(base.input_fingerprint_sha256,
                                    alternate.input_fingerprint_sha256)

    def test_research_fingerprint_distinguishes_unpinned_and_pinned_calendar(self):
        dates = tuple(item.bar.day for item in self.spy.observations)
        digest = session_manifest_sha256(dates)
        def replay(days=None, pinned=None):
            return replay_candidate_snapshot(
                self.spy, "buy_hold", expected_sha256=self.spy.sha256,
                expected_session_dates=days,
                expected_session_manifest_sha256=pinned)
        absent = replay()
        unpinned = replay(dates)
        pinned = replay(dates, digest)
        self.assertNotEqual(absent.input_fingerprint_sha256,
                            unpinned.input_fingerprint_sha256)
        self.assertNotEqual(unpinned.input_fingerprint_sha256,
                            pinned.input_fingerprint_sha256)
        self.assertEqual(pinned.input_fingerprint_sha256,
                         replay(list(dates), digest).input_fingerprint_sha256)

    def test_comparison_fingerprint_binds_both_sources_and_comparison_kind(self):
        aapl = candidate("AAPL", asset_type="STOCK")
        def compare(spy):
            return compare_candidate_snapshots(
                aapl, spy, "trend",
                expected_strategy_sha256=aapl.sha256,
                expected_spy_sha256=spy.sha256,
                lookback=5)
        first = compare(self.spy)
        second = compare(self.spy)
        different_spy = inspect_daily_snapshot(
            "SPY", self.spy.observations, asset_type="ETF",
            source=self.spy.source,
            source_revision="offline_fixture_v2",
            price_basis=self.spy.price_basis, as_of=self.spy.as_of)
        self.assertEqual(first.input_fingerprint_sha256,
                         second.input_fingerprint_sha256)
        self.assertNotEqual(first.input_fingerprint_sha256,
                            compare(different_spy).input_fingerprint_sha256)
        single = replay_candidate_snapshot(
            aapl, "trend", expected_sha256=aapl.sha256, lookback=5)
        self.assertNotEqual(first.input_fingerprint_sha256,
                            single.input_fingerprint_sha256)


if __name__ == "__main__":
    unittest.main()
