"""Deterministic unit tests for the read-only offline quote freshness gate."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import unittest

from alphora.quote_freshness import QuoteObservation, assess_quote


NOW = datetime(2026, 11, 9, 15, 30, tzinfo=timezone.utc)


def quote(*, age=6, receive_age=2):
    return QuoteObservation("SPY", D("500"), D("499.99"), D("500.01"),
                            NOW - timedelta(seconds=age),
                            NOW - timedelta(seconds=receive_age), "SIP-test")


class QuoteFreshnessTests(unittest.TestCase):
    def check(self, sample=None, **kwargs):
        return assess_quote(quote() if sample is None else sample,
                            expected_symbol="SPY", evaluated_at=NOW,
                            market_session="REGULAR", **kwargs)

    def test_cadence_and_freshness_are_different(self):
        for purpose, poll, age_limit in (
            ("POSITION", 5, 10),
            ("WATCHLIST", 15, 30),
            ("DISCOVERY", 60, 120),
        ):
            with self.subTest(purpose=purpose):
                decision = self.check(quote(age=age_limit, receive_age=0),
                                      purpose=purpose)
                self.assertTrue(decision.usable)
                self.assertEqual(decision.reason, "FRESH_CALLER_SUPPLIED_QUOTE")
                self.assertEqual(decision.suggested_poll_seconds, poll)
                self.assertEqual(decision.quote_age_seconds, D(age_limit))
                self.assertEqual(decision.last_price, D("500"))
                stale = self.check(
                    quote(age=age_limit + 0.000001, receive_age=0),
                    purpose=purpose)
                self.assertFalse(stale.usable)
                self.assertEqual(stale.reason, "STALE_QUOTE")
                self.assertIsNone(stale.last_price)

    def test_default_position_review_is_five_seconds(self):
        result = self.check()
        self.assertTrue(result.usable)
        self.assertEqual(result.suggested_poll_seconds, 5)
        self.assertEqual(result.quote_age_seconds, D("6"))

    def test_explicit_non_regular_and_unknown_session_are_blocked(self):
        for status in ("PRE", "POST", "CLOSED", "UNKNOWN", None, ""):
            with self.subTest(status=status):
                result = assess_quote(quote(), expected_symbol="SPY",
                                      evaluated_at=NOW, market_session=status)
                self.assertFalse(result.usable)
                self.assertEqual(result.reason, "REGULAR_SESSION_REQUIRED")

    def test_live_mode_always_rejected(self):
        for mode in ("live", "real", None, ""):
            with self.subTest(mode=mode):
                result = self.check(mode=mode)
                self.assertFalse(result.usable)
                self.assertEqual(result.reason, "REAL_TRADING_DISABLED")

    def test_missing_and_symbol_mismatch(self):
        missing = assess_quote(None, expected_symbol="SPY",
                               evaluated_at=NOW, market_session="REGULAR")
        self.assertEqual(missing.reason, "MISSING_QUOTE")
        result = assess_quote(quote(), expected_symbol="QQQ",
                              evaluated_at=NOW, market_session="REGULAR")
        self.assertEqual(result.reason, "SYMBOL_MISMATCH")

    def test_reject_nonfinite_negative_or_crossed_prices(self):
        invalid = (
            replace(quote(), last_price=D("NaN")),
            replace(quote(), last_price=D("Infinity")),
            replace(quote(), bid=D("-1")),
            replace(quote(), bid=D("501")),
            replace(quote(), ask=D("0")),
            replace(quote(), ask=D("NaN")),
            replace(quote(), last_price=500),
        )
        for case in invalid:
            with self.subTest(case=case):
                result = self.check(case)
                self.assertFalse(result.usable)
                self.assertEqual(result.reason, "INVALID_QUOTE_PRICES")

    def test_feed_label_required_but_not_authenticated(self):
        for label in ("", " ", "vendor/url", None):
            with self.subTest(label=label):
                self.assertEqual(self.check(replace(quote(), feed_label=label)).reason,
                                 "MISSING_FEED_PROVENANCE")

    def test_unordered_future_timestamps_fail_closed(self):
        invalid = (
            quote(age=-1, receive_age=0),
            quote(age=1, receive_age=-1),
            quote(age=1, receive_age=3),
        )
        for case in invalid:
            with self.subTest(case=case):
                result = self.check(case)
                self.assertFalse(result.usable)
                self.assertEqual(result.reason, "TIMESTAMP_ORDER_INVALID")
        self.assertEqual(self.check(replace(quote(), market_at=NOW.replace(tzinfo=None))).reason,
                         "INVALID_QUOTE_TIMES")
        self.assertEqual(self.check(replace(quote(), received_at=NOW.replace(tzinfo=None))).reason,
                         "INVALID_QUOTE_TIMES")

    def test_naive_evaluation_time_and_unrecognized_purpose(self):
        result = assess_quote(quote(), expected_symbol="SPY",
                              evaluated_at=NOW.replace(tzinfo=None),
                              market_session="REGULAR")
        self.assertEqual(result.reason, "INVALID_EVALUATION_TIME")
        result = self.check(purpose="ORDER")
        self.assertFalse(result.usable)
        self.assertEqual(result.reason, "INVALID_MONITOR_PURPOSE")
        self.assertEqual(result.suggested_poll_seconds, 5)

    def test_dst_fallback_uses_absolute_timeline(self):
        # At the fall-back, local clock time moves from 01:59:55 EDT
        # to 01:00:05 EST while only ten real seconds elapse.
        tz = ZoneInfo("America/New_York")
        market_at = datetime(2026, 11, 1, 1, 59, 55, tzinfo=tz, fold=0)
        received_at = datetime(2026, 11, 1, 1, 0, 3, tzinfo=tz, fold=1)
        checked_at = datetime(2026, 11, 1, 1, 0, 5, tzinfo=tz, fold=1)
        sample = replace(quote(), market_at=market_at,
                         received_at=received_at)
        decision = assess_quote(sample, expected_symbol="SPY",
                                evaluated_at=checked_at,
                                market_session="REGULAR")
        self.assertTrue(decision.usable)
        self.assertEqual(decision.quote_age_seconds, D("10"))
        self.assertEqual(decision.suggested_poll_seconds, 5)

    def test_cross_timezone_consistency(self):
        newyork = timezone(timedelta(hours=-5))
        observed = quote()
        with_offset = replace(observed,
                              received_at=observed.received_at.astimezone(newyork),
                              market_at=observed.market_at.astimezone(newyork))
        self.assertEqual(self.check(with_offset), self.check())


if __name__ == "__main__":
    unittest.main()
