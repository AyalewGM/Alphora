"""Offline synthetic fixtures only. No equities provider data or broker paths."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import unittest
from zoneinfo import ZoneInfo

from alphora.research.corporate_actions import (
    SessionClose, Split, CashDividend, CashDelisting, replay_static_position,
)
ET = ZoneInfo("America/New_York")


def et(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


class CorporateActionsTests(unittest.TestCase):
    def setUp(self):
        self.d = [date(2025, 3, 3) + timedelta(days=i) for i in range(5)]
        self.s = tuple(SessionClose(d, et(d, 9, 30), et(d, 16),
                        D(p) if p is not None else None,
                        et(d, 16, 5))
                       for d, p in zip(self.d, ("100", "50", "49", "98", None)))
        first_known = datetime(2025, 2, 20, tzinfo=timezone.utc)
        self.events = (
            Split("s2", "asset-1", self.d[1], first_known, first_known, 2, 1),
            CashDividend("div", "asset-1", self.d[2], self.d[3],
                         first_known, first_known, D("1")),
            Split("rev", "asset-1", self.d[3], first_known, first_known, 1, 2),
            CashDelisting("exit", "asset-1", self.d[4], first_known, first_known,
                          D("98")),
        )
        self.defaults = dict(asset_id="asset-1", sessions=self.s,
                             actions=self.events, initial_cash=D("100"),
                             initial_shares=10, initial_cost_basis=D("600"),
                             as_of=et(self.d[-1], 17),
                             price_basis="RAW_UNADJUSTED")

    def replay(self, **kwargs):
        return replay_static_position(**(self.defaults | kwargs))

    def assert_code(self, code, **kwargs):
        with self.assertRaisesRegex(ValueError, code):
            self.replay(**kwargs)

    def test_split_dividend_reverse_split_and_delisting_conserve_value(self):
        d = self.replay().days
        self.assertEqual([x.shares for x in d], [10, 20, 20, 10, 0])
        self.assertEqual([x.cash for x in d], [D("100"), D("100"), D("100"),
                                              D("120"), D("1100")])
        self.assertEqual([x.dividend_receivable for x in d],
                         [D(0), D(0), D(20), D(0), D(0)])
        self.assertEqual([x.gross_value for x in d], [D("1100")] * 5)
        self.assertEqual([x.held_cost_basis for x in d],
                         [D(600), D(600), D(600), D(600), D(0)])
        self.assertEqual(d[-1].retired_cost_basis, D("600"))
        self.assertTrue(d[-1].closed)

    def test_synthetic_early_close_accepted(self):
        early = replace(self.s[2], closes_at=et(self.d[2], 13),
                        close_available_at=et(self.d[2], 13, 5))
        self.assertEqual(self.replay(sessions=self.s[:2] + (early,) + self.s[3:]).days[-1].gross_value,
                         D("1100"))

    def test_deterministic_replay(self):
        self.assertEqual(self.replay(), self.replay())

    def test_vendor_correction_after_next_open_rejected(self):
        late = replace(self.s[1], close_available_at=et(self.d[2], 10))
        self.assert_code("INVALID_OR_LATE_SESSION_MARK", sessions=self.s[:1] + (late,) + self.s[2:])

    def test_hindsight_split_rejected(self):
        future = replace(self.events[0], available_at=et(self.d[1], 10))
        self.assert_code("EVENT_CONFLICT_OR_LOOKAHEAD", actions=(future,) + self.events[1:])

    def test_duplicate_or_conflicting_event_rejected(self):
        self.assert_code("EVENT_CONFLICT_OR_LOOKAHEAD", actions=self.events + (self.events[0],))
        self.assert_code("EVENT_CONFLICT_OR_LOOKAHEAD", actions=self.events +
                         (replace(self.events[0], event_id="another"),))

    def test_missing_session_rejected(self):
        self.assert_code("EVENT_CONFLICT_OR_LOOKAHEAD", sessions=self.s[:2] + self.s[3:])

    def test_ambiguous_price_adjustment_rejected(self):
        for p in ("SPLIT_ADJUSTED", "TOTAL_RETURN_ADJUSTED", None):
            with self.subTest(p=p):
                self.assert_code("INVALID_REPLAY_INPUT", price_basis=p)

    def test_missing_raw_mark_rejected(self):
        self.assert_code("MISSING_RAW_MARK", sessions=(replace(self.s[0], raw_close=None),) + self.s[1:])

    def test_unknown_delisting_consideration_fails_closed(self):
        e = replace(self.events[-1], settlement_per_share=None)
        self.assert_code("UNKNOWN_DELISTING_CONSIDERATION", actions=self.events[:-1] + (e,))

    def test_unannounced_or_unavailable_dividend_rejected(self):
        e = replace(self.events[1], announced_at=et(self.d[2], 10),
                    available_at=et(self.d[2], 10))
        self.assert_code("EVENT_CONFLICT_OR_LOOKAHEAD", actions=self.events[:1] + (e,) + self.events[2:])

    def test_fractional_reverse_split_requires_cash_in_lieu(self):
        e = replace(self.events[2], numerator=1, denominator=3)
        self.assert_code("CASH_IN_LIEU_NOT_MODELED", actions=self.events[:2] + (e,) + self.events[3:])

    def test_boolean_split_ratio_and_negative_cash_invalid(self):
        e = replace(self.events[0], numerator=True)
        self.assert_code("INVALID_SPLIT_RATIO", actions=(e,) + self.events[1:])
        self.assert_code("INVALID_REPLAY_INPUT", initial_cash=D("-1"))

    def test_no_future_session_after_cash_delisting(self):
        e = replace(self.events[-1], effective_on=self.d[3])
        # Delisting conflicts with reverse split on this date.
        self.assert_code("EVENT_CONFLICT_OR_LOOKAHEAD", actions=self.events[:-1] + (e,))

    def test_reordered_sessions_rejected(self):
        self.assert_code("INVALID_OR_LATE_SESSION_MARK", sessions=self.s[:1] + self.s[2:3] + self.s[1:2] + self.s[3:])

    def test_delisting_zero_known_vs_unknown(self):
        a = replace(self.events[-1], settlement_per_share=D(0))
        r = self.replay(actions=self.events[:-1] + (a,))
        self.assertEqual(r.days[-1].cash, D(120))
        self.assertTrue(r.days[-1].closed)


if __name__ == "__main__":
    unittest.main()
