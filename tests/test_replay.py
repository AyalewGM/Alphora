"""Offline-only synthetic replay contract; never touches market or broker APIs."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.research.backtest import Bar, backtest
from alphora.research.data_provenance import ObservedDailyBar, inspect_daily_snapshot
from alphora.research.replay import (
    replay_candidate_snapshot, compare_candidate_snapshots
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


if __name__ == "__main__":
    unittest.main()
