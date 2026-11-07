import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.research.gap_scanner import GapCandidate, ScannerPolicy, screen_one, scan

ET = ZoneInfo("America/New_York")


def fixture(**changes):
    base = GapCandidate(
        symbol="TEST", as_of=datetime(2026, 11, 2, 8, 30, tzinfo=ET),
        session="PREMARKET", listing="NASDAQ", security_type="COMMON",
        feed="SIP", source="synthetic-fixture",
        previous_close=D("10"), previous_close_at=datetime(2026, 10, 30, 16, tzinfo=ET),
        previous_session_date=date(2026, 10, 30),
        previous_session_close_at=datetime(2026, 10, 30, 16, tzinfo=ET),
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


    def test_strict_boolean_attestations(self):
        for value in ('false','no',1):
            result=screen_one(fixture(calendar_verified=value,
                                      corporate_actions_verified=value))
            self.assertEqual(result.classification,'EXCLUDED')
            self.assertIn('UNVERIFIED_TRADING_CALENDAR',result.reasons)
            self.assertIn('UNVERIFIED_PRICE_ADJUSTMENTS',result.reasons)

    def test_stale_history_and_nonregular_previous_close(self):
        old=datetime(2026,10,1,16,tzinfo=ET)
        result=screen_one(fixture(previous_close_at=old,history_through=date(2026,10,1)))
        self.assertIn('PREVIOUS_CLOSE_NOT_REGULAR_SESSION',result.reasons)
        self.assertIn('STALE_OR_MISALIGNED_HISTORY',result.reasons)
        after=datetime(2026,10,30,19,45,tzinfo=ET)
        result=screen_one(fixture(previous_close_at=after))
        self.assertIn('PREVIOUS_CLOSE_NOT_REGULAR_SESSION',result.reasons)
        self.assertEqual(result.classification,'EXCLUDED')

    def test_wrong_calendar_previous_session_date_fails(self):
        result=screen_one(fixture(previous_session_date=date(2026,10,29)))
        self.assertIn('UNVERIFIED_PREVIOUS_SESSION',result.reasons)
        self.assertIn('STALE_OR_MISALIGNED_HISTORY',result.reasons)

    def test_previously_published_catalyst_requires_caution(self):
        old=datetime(2026,8,1,8,30,tzinfo=ET)
        result=screen_one(fixture(credible_catalyst_at=old))
        self.assertEqual(result.classification,'CAUTION')
        self.assertIn('NO_TIMELY_CATALYST',result.reasons)
        at_close=datetime(2026,10,30,16,tzinfo=ET)
        self.assertIn('NO_TIMELY_CATALYST',screen_one(fixture(credible_catalyst_at=at_close)).reasons)

    def test_scanner_policy_validation_rejects_unsafe_settings(self):
        for kwargs in ({'min_rvol':D(-1)}, {'max_quote_age_seconds':10**9},
                       {'max_quote_age_seconds':True}, {'min_adv20':False}):
            with self.assertRaisesRegex(ValueError,'INVALID_SCANNER_POLICY'):
                ScannerPolicy(**kwargs)

    def test_malformed_symbols_do_not_crash_batch_sorting(self):
        items=[fixture(symbol=None,avg_daily_dollar_volume20=D(1)),
               fixture(symbol='AAAA',avg_daily_dollar_volume20=D(1))]
        self.assertEqual(len(scan(items)),2)
        self.assertTrue(all(x.classification=='EXCLUDED' for x in scan(items)))


    def test_extreme_finite_decimal_inputs_fail_closed_without_batch_crash(self):
        # Decimal.is_finite() is not sufficient for arithmetic safety:
        # values beyond the active context can raise decimal.Overflow.
        cases = (
            ({"previous_close": D("1e-999999999")}, "INVALID_PRICE_ARITHMETIC"),
            ({"ask": D("1e999999999")}, "INVALID_SPREAD_ARITHMETIC"),
            ({"avg_daily_dollar_volume20": D("1e999999999")},
             "INVALID_DOLLAR_VOLUME_ARITHMETIC"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                result = screen_one(fixture(**changes))
                self.assertEqual(result.classification, "EXCLUDED")
                self.assertIn(reason, result.reasons)
                self.assertEqual(
                    scan([fixture(symbol="SAFE"), fixture(symbol="BAD", **changes)])[0].symbol,
                    "SAFE",
                )


    def test_decimal_rounding_cannot_promote_below_threshold_candidates(self):
        # With default Decimal precision, ordinary half-even rounding makes
        # the 2.999...% gap appear exactly 3% and wrongly pass the minimum.
        below_gap = fixture(reference_price=D("10.299999999999999999999999999"))
        self.assertEqual(
            (below_gap.reference_price / below_gap.previous_close - 1) * 100, D("3")
        )
        result = screen_one(below_gap)
        self.assertEqual(result.classification, "EXCLUDED")
        self.assertIn("GAP_BELOW_MINIMUM", result.reasons)

        # Extremely large, integer-only but untrusted feed volumes can also
        # round a true sub-2x RVOL to exactly 2x without a Decimal exception.
        baseline = 10 ** 34
        below_rvol = fixture(
            premarket_volume=2 * baseline - 1,
            same_time_baseline_volume=baseline,
        )
        self.assertEqual(
            D(below_rvol.premarket_volume) / D(below_rvol.same_time_baseline_volume),
            D("2"),
        )
        result = screen_one(below_rvol)
        self.assertEqual(result.classification, "EXCLUDED")
        self.assertIn("LOW_SAME_TIME_RVOL", result.reasons)

        # Exactly on the policy boundary must still pass.
        on_boundary = fixture(
            reference_price=D("10.30"), premarket_volume=300_000,
            same_time_baseline_volume=150_000,
        )
        self.assertEqual(screen_one(on_boundary).classification, "WATCHLIST")
        self.assertEqual(scan([below_gap, below_rvol, on_boundary])[0].symbol,
                         on_boundary.symbol)


    def test_source_identifier_requires_nonblank_text(self):
        for source in ("", "  ", None, 42):
            with self.subTest(source=source):
                result = screen_one(fixture(source=source))
                self.assertEqual(result.classification, "EXCLUDED")
                self.assertIn("MISSING_MARKET_PROVENANCE", result.reasons)

    def test_unknown_reversal_or_dilution_evidence_is_not_clean(self):
        for field, reason in (
            ("reversal_signal", "REVERSAL_EVIDENCE_UNKNOWN"),
            ("dilution_signal", "DILUTION_EVIDENCE_UNKNOWN"),
        ):
            for value in (None, 0, "false"):
                with self.subTest(field=field, value=value):
                    result = screen_one(fixture(**{field: value}))
                    self.assertEqual(result.classification, "CAUTION")
                    self.assertIn(reason, result.reasons)

        for field, reason in (
            ("reversal_signal", "SHARP_REVERSAL_FLAG"),
            ("dilution_signal", "RECENT_DILUTION_FLAG"),
        ):
            with self.subTest(field=field, value=True):
                result = screen_one(fixture(**{field: True}))
                self.assertEqual(result.classification, "CAUTION")
                self.assertIn(reason, result.reasons)
            self.assertEqual(screen_one(fixture(**{field: False})).classification,
                             "WATCHLIST")

    def test_spread_just_above_limit_cannot_round_into_watchlist(self):
        # The true spread exceeds 1%, but normal Decimal precision rounds
        # the displayed ratio to precisely 1%, falsely permitting a watchlist.
        above = fixture(
            previous_close=D("200"), reference_price=D("206"),
            bid=D("199"), ask=D("201." + "0" * 27 + "1"),
        )
        self.assertEqual(
            (above.ask - above.bid) * 200 / (above.ask + above.bid), D("1")
        )
        result = screen_one(above)
        self.assertEqual(result.classification, "EXCLUDED")
        self.assertIn("WIDE_SPREAD", result.reasons)

        # Exact 1% remains eligible: conservative upper-bound arithmetic
        # must not invent an exclusion at the actual limit.
        exact = replace(above, ask=D("201"))
        self.assertEqual(screen_one(exact).classification, "WATCHLIST")
        self.assertEqual(
            scan([above, exact])[0].classification, "WATCHLIST"
        )


if __name__ == "__main__":
    unittest.main()
