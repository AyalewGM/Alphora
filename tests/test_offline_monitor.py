"""Fail-closed offline monitoring schedule: premarket, regular, DST, holidays."""
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from alphora.offline_monitor import (
    ET, VerifiedSession, MonitorCheckpoint, MonitorQuote, plan_monitor_tick,
)

DAY = date(2026, 11, 9)


def instant(hour, minute=0, second=0, *, day=DAY):
    return datetime.combine(day, time(hour, minute, second), tzinfo=ET)


def session(*, day=DAY, early=False, verified=True, halted=False):
    return VerifiedSession(
        day=day, opens_at=instant(9, 30, day=day),
        closes_at=instant(13 if early else 16, day=day),
        calendar_verified=verified, trading_halted=halted,
    )


def checkpoint(*, day=DAY, **changes):
    return MonitorCheckpoint(day, **changes)


def quote(*, at=None, bid=D("50"), ask=D("50.10"),
          source_verified=True, symbol="SPY"):
    return MonitorQuote(
        symbol, bid, ask, at or instant(10), source_verified)


class OfflineMonitorTests(unittest.TestCase):
    def test_approved_hours_boundaries(self):
        for hour, minute, expect in (
            (3, 59, "CLOSED"),
            (4, 0, "PREMARKET"),
            (9, 29, "PREMARKET"),
            (9, 30, "REGULAR"),
            (15, 59, "REGULAR"),
            (16, 0, "CLOSED"),
            (20, 0, "CLOSED"),
        ):
            with self.subTest(hour=hour, minute=minute):
                r = plan_monitor_tick(now=instant(hour, minute), session=session(),
                                      checkpoint=checkpoint())
                self.assertEqual(r.session, expect)
                if expect != "REGULAR":
                    self.assertFalse(r.regular_exit_evaluation_allowed)
        pre = plan_monitor_tick(now=instant(4), session=session(),
                                checkpoint=checkpoint(), held_quote=quote(at=instant(4)))
        self.assertTrue(pre.positions_due)
        self.assertTrue(pre.discovery_due)
        self.assertFalse(pre.regular_exit_evaluation_allowed)

    def test_quote_freshness_and_spread_for_regular_only(self):
        now = instant(10)
        valid = plan_monitor_tick(now=now, session=session(),
                                  checkpoint=checkpoint(), held_quote=quote(at=now))
        self.assertEqual(valid.reason, "REGULAR_QUOTE_VALID")
        self.assertTrue(valid.regular_exit_evaluation_allowed)
        for q in (
            quote(at=now - timedelta(seconds=6)),
            quote(at=now + timedelta(seconds=1)),
            quote(at=now, bid=D("49"), ask=D("51")),
            quote(at=now, bid=D("NaN")),
            quote(at=now, source_verified=False),
            quote(at=now, symbol="---"),
            quote(at=now, bid=D("0")),
            None,
        ):
            with self.subTest(q=q):
                blocked = plan_monitor_tick(now=now, session=session(),
                                            checkpoint=checkpoint(), held_quote=q)
                self.assertTrue(blocked.positions_due)
                self.assertFalse(blocked.regular_exit_evaluation_allowed)

    def test_spread_above_limit_cannot_round_to_eligible(self):
        now = instant(10)
        exact = plan_monitor_tick(
            now=now, session=session(), checkpoint=checkpoint(),
            held_quote=quote(at=now, bid=D("199"), ask=D("201")))
        self.assertTrue(exact.regular_exit_evaluation_allowed)
        # Sub-cent precision beyond the arithmetic context must never
        # round a strictly >1% spread back to the allowed 1% boundary.
        too_wide = plan_monitor_tick(
            now=now, session=session(), checkpoint=checkpoint(),
            held_quote=quote(at=now, bid=D("199"),
                             ask=D("201." + "0" * 52 + "1")))
        self.assertFalse(too_wide.regular_exit_evaluation_allowed)

    def test_due_intervals_and_future_checkpoint_fail_closed(self):
        now = instant(10)
        c = checkpoint(position_at=now-timedelta(seconds=4),
                       watchlist_at=now-timedelta(seconds=15),
                       discovery_at=now-timedelta(seconds=59),
                       news_at=now-timedelta(seconds=300))
        r = plan_monitor_tick(now=now, session=session(), checkpoint=c,
                              held_quote=quote(at=now))
        self.assertFalse(r.positions_due)
        self.assertTrue(r.watchlist_due)
        self.assertFalse(r.discovery_due)
        self.assertTrue(r.news_due)
        self.assertFalse(r.regular_exit_evaluation_allowed)
        for bad in (
            checkpoint(position_at=now + timedelta(seconds=1)),
            checkpoint(position_at=now - timedelta(days=1)),
            checkpoint(position_at=datetime(2026,11,9,10)),
            checkpoint(day=date(2026, 11, 8)),
            None,
        ):
            with self.subTest(c=bad):
                result = plan_monitor_tick(now=now, session=session(), checkpoint=bad)
                self.assertEqual(result.session, "BLOCKED")
                self.assertFalse(result.positions_due)

    def test_reject_unauthenticated_calendar_holiday_and_halt(self):
        now = instant(10)
        for bad in (
            None,
            session(verified=False),
            replace(session(), day=date(2026, 11, 8)),
            replace(session(), opens_at=instant(10)),
            replace(session(), closes_at=instant(20)),
            replace(session(), calendar_verified=1),
            replace(session(), trading_halted="false"),
        ):
            with self.subTest(bad=bad):
                r = plan_monitor_tick(now=now, session=bad,
                                      checkpoint=checkpoint(), held_quote=quote(at=now))
                self.assertEqual(r.session, "BLOCKED")
                self.assertFalse(r.regular_exit_evaluation_allowed)
        stopped = plan_monitor_tick(now=now, session=session(halted=True),
                                    checkpoint=checkpoint(), held_quote=quote(at=now))
        self.assertEqual(stopped.session, "HALTED")
        self.assertFalse(stopped.positions_due)

    def test_early_close_boundary_and_dst(self):
        half = session(early=True)
        self.assertEqual(plan_monitor_tick(
            now=instant(12, 59), session=half, checkpoint=checkpoint()).session,
            "REGULAR")
        self.assertEqual(plan_monitor_tick(
            now=instant(13), session=half, checkpoint=checkpoint()).session,
            "CLOSED")
        # Winter and summer independently attested sessions must preserve
        # New York exchange wall times through DST changes.
        summer = date(2026, 7, 6)
        summer_now = instant(9, 30, day=summer)
        result = plan_monitor_tick(now=summer_now.astimezone(timezone.utc),
                                   session=session(day=summer),
                                   checkpoint=checkpoint(day=summer),
                                   held_quote=quote(at=summer_now))
        self.assertEqual(result.session, "REGULAR")
        self.assertTrue(result.regular_exit_evaluation_allowed)
        self.assertEqual(summer_now.utcoffset(), timedelta(hours=-4))
        self.assertEqual(instant(9,30).utcoffset(), timedelta(hours=-5))

    def test_invalid_clock_and_no_order_surface(self):
        for invalid in (None, "2026-11-09", datetime(2026,11,9,10)):
            r = plan_monitor_tick(now=invalid, session=session(),
                                  checkpoint=checkpoint())
            self.assertEqual(r.session, "BLOCKED")
        import alphora.offline_monitor as mod
        for name in ("submit_order","record_hypothetical_sell","connect_alpaca",
                     "fetch_quotes","execute_trade","sleep"):
            self.assertFalse(hasattr(mod,name))


if __name__ == "__main__":
    unittest.main()
