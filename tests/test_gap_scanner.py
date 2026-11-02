import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.research.gap_scanner import GapCandidate, screen_one, scan

ET = ZoneInfo("America/New_York")


def fixture(**changes):
    base = GapCandidate(
        symbol="TEST", as_of=datetime(2026, 11, 2, 8, 30, tzinfo=ET),
        session="PREMARKET", listing="NASDAQ", security_type="COMMON",
        feed="SIP", source="synthetic-fixture",
        previous_close=D("10"), previous_close_at=datetime(2026, 10, 30, 16, tzinfo=ET),
        reference_price=D("10.60"), reference_price_at=datetime(2026, 11, 2, 8, 29, tzinfo=ET),
        history_through=date(2026, 10, 30), adv20=1_500_000,
        avg_daily_dollar_volume20=D("25000000"),
        premarket_volume=300_000, regular_cumulative_volume=0,
        same_time_baseline_volume=100_000,
        bid=D("10.58"), ask=D("10.62"),
        quote_at=datetime(2026, 11, 2, 8, 29, 30, tzinfo=ET),
        calendar_verified=True, corporate_actions_verified=True,
        credible_catalyst_at=datetime(2026, 11, 2, 7, 0, tzinfo=ET),
        free_float_shares=50_000_000, promotion_signal=False,
    )
    return replace(base, **changes)


class ScannerTests(unittest.TestCase):
    def test_healthy_gap_and_liquidity_is_research_watchlist(self):
        result = screen_one(fixture())
        self.assertEqual(result.classification, "WATCHLIST")
        self.assertEqual(result.gap_pct, D("6.00"))
        self.assertEqual(result.rvol, D("3"))
        self.assertLess(result.spread_pct, D("1"))

    def test_promotional_stock_excluded_even_with_good_volume(self):
        result = screen_one(fixture(promotion_signal=True,
                                    premarket_volume=1_200_000, reference_price=D("11.8")))
        self.assertEqual(result.classification, "EXCLUDED")
        self.assertIn("PROMOTIONAL_ACTIVITY_FLAG", result.reasons)

    def test_missing_catalyst_is_caution_not_fraud_conclusion(self):
        result = screen_one(fixture(credible_catalyst_at=None))
        self.assertEqual(result.classification, "CAUTION")
        self.assertIn("NO_VERIFIED_CATALYST", result.reasons)

    def test_unknown_float_and_social_signal_are_caution(self):
        result = screen_one(fixture(free_float_shares=None, promotion_signal=None))
        self.assertEqual(result.classification, "CAUTION")
        self.assertIn("FREE_FLOAT_UNKNOWN", result.reasons)
        self.assertIn("PROMOTION_EVIDENCE_UNKNOWN", result.reasons)

    def test_iex_only_feed_cannot_get_top_watchlist_classification(self):
        self.assertEqual(screen_one(fixture(feed="IEX")).classification, "CAUTION")

    def test_partial_premarket_is_not_compared_against_full_day(self):
        self.assertEqual(screen_one(fixture(same_time_baseline_volume=None)).classification, "CAUTION")
        self.assertEqual(screen_one(fixture(same_time_baseline_volume=250_000)).classification, "EXCLUDED")

    def test_data_and_timestamp_lookahead_rejected(self):
        now = fixture().as_of
        for candidate in [
            fixture(history_through=date(2026, 11, 2)),
            fixture(credible_catalyst_at=now + timedelta(minutes=1)),
            fixture(reference_price_at=now + timedelta(seconds=1)),
            fixture(quote_at=now + timedelta(seconds=1)),
        ]:
            self.assertEqual(screen_one(candidate).classification, "EXCLUDED")

    def test_stale_quote_and_wide_spread_excluded(self):
        self.assertIn("STALE_OR_FUTURE_QUOTE",
                      screen_one(fixture(quote_at=datetime(2026,11,2,8,20,tzinfo=ET))).reasons)
        self.assertIn("WIDE_SPREAD",
                      screen_one(fixture(bid=D("10"), ask=D("10.8"))).reasons)

    def test_gap_price_adv_and_dollar_volume_thresholds(self):
        for candidate in [
            fixture(reference_price=D("10.1")),
            fixture(reference_price=D("4.9")),
            fixture(adv20=999_999),
            fixture(avg_daily_dollar_volume20=D("19999999")),
            fixture(premarket_volume=99_999),
        ]:
            self.assertEqual(screen_one(candidate).classification, "EXCLUDED")

    def test_noncommon_otc_and_unverified_adjustment_excluded(self):
        for candidate in [
            fixture(listing="OTC"), fixture(security_type="ETF"),
            fixture(corporate_actions_verified=False),
            fixture(calendar_verified=False),
        ]:
            self.assertEqual(screen_one(candidate).classification, "EXCLUDED")

    def test_open_gap_uses_open_price_and_regular_rvol(self):
        candidate=fixture(
            session="OPEN", as_of=datetime(2026,11,2,9,35,tzinfo=ET),
            reference_price_at=datetime(2026,11,2,9,30,15,tzinfo=ET),
            quote_at=datetime(2026,11,2,9,34,30,tzinfo=ET),
            regular_cumulative_volume=200_000,
            same_time_baseline_volume=80_000,
        )
        self.assertEqual(screen_one(candidate).classification, "WATCHLIST")
        self.assertEqual(screen_one(replace(candidate,
                  reference_price_at=datetime(2026,11,2,9,34,tzinfo=ET))).classification, "EXCLUDED")

    def test_nan_and_naive_event_time_fail_closed(self):
        self.assertEqual(screen_one(fixture(reference_price=D("NaN"))).classification, "EXCLUDED")
        self.assertEqual(screen_one(fixture(as_of=datetime(2026,11,2,8,30))).classification, "EXCLUDED")

    def test_ranking_prioritizes_watchlist_then_liquidity(self):
        results = scan([
            fixture(symbol="BBBB", avg_daily_dollar_volume20=D("40000000")),
            fixture(symbol="CCCC", promotion_signal=True,
                    avg_daily_dollar_volume20=D("90000000")),
            fixture(symbol="AAAA", avg_daily_dollar_volume20=D("50000000")),
        ])
        self.assertEqual([x.symbol for x in results], ["AAAA", "BBBB", "CCCC"])
        self.assertEqual(results[-1].classification, "EXCLUDED")


if __name__ == "__main__":
    unittest.main()
