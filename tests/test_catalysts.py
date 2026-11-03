"""Synthetic, offline point-in-time catalyst tests. No market data or API calls."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from alphora.research.catalysts import CatalystEvent, assess_catalysts

UTC = timezone.utc
CLOSE = datetime(2026, 11, 2, 21, tzinfo=UTC)
NOW = CLOSE + timedelta(hours=16)


def event(**changes):
    base = CatalystEvent(
        event_id="release-1", symbol="TEST", category="EARNINGS",
        source_kind="COMPANY_RELEASE", direction="POSITIVE",
        source_url="https://investor.example.org/earnings", publisher="TEST IR",
        published_at=CLOSE + timedelta(hours=1),
        first_seen_at=CLOSE + timedelta(hours=1, minutes=5),
        received_at=CLOSE + timedelta(hours=1, minutes=6),
        source_origin_verified=True, promotional=False,
    )
    return replace(base, **changes)


def assess(*events, as_of=NOW):
    return assess_catalysts("TEST", as_of=as_of,
                            previous_session_close_at=CLOSE, events=list(events))


class CatalystAssessmentTests(unittest.TestCase):
    def test_attested_primary_is_evidence_not_trade_authorization(self):
        result = assess(event())
        self.assertEqual(result.classification, "PRIMARY_EVENT")
        self.assertEqual(result.entry_catalyst_status, "REVIEW_REQUIRED")
        self.assertEqual(len(result.evidence), 1)
        self.assertTrue(result.evidence[0].primary_source_attested)

    def test_first_received_after_decision_excludes_retroactive_headline(self):
        late = event(received_at=NOW + timedelta(seconds=1))
        result = assess(late)
        self.assertEqual(result.classification, "NO_TIMELY_EVENT")
        self.assertIn("FUTURE_INFORMATION_EXCLUDED", result.reasons)
        self.assertEqual(result.evidence, ())

    def test_publication_not_enough_to_prove_point_in_time_access(self):
        late = event(first_seen_at=NOW + timedelta(minutes=1),
                     received_at=NOW + timedelta(minutes=2))
        self.assertEqual(assess(late).evidence, ())

    def test_backdated_publication_does_not_qualify_current_gap(self):
        old = event(published_at=CLOSE - timedelta(hours=2),
                    first_seen_at=CLOSE - timedelta(hours=1),
                    received_at=CLOSE - timedelta(minutes=30))
        result = assess(old)
        self.assertEqual(result.classification, "NO_TIMELY_EVENT")
        self.assertIn("OLDER_THAN_PREVIOUS_CLOSE", result.reasons)

    def test_news_is_not_equivalent_to_primary_attestation(self):
        news = event(source_kind="PROFESSIONAL_NEWS")
        result = assess(news)
        self.assertEqual(result.classification, "UNVERIFIED_EVENT")
        self.assertFalse(result.evidence[0].primary_source_attested)

    def test_unattested_primary_requires_review(self):
        result = assess(event(source_origin_verified=False))
        self.assertEqual(result.classification, "REVIEW_REQUIRED")
        self.assertIn("SOURCE_NOT_ATTESTED", result.reasons)

    def test_promotional_positive_event_cannot_be_primary_only(self):
        result = assess(event(promotional=True))
        self.assertEqual(result.classification, "REVIEW_REQUIRED")
        self.assertIn("PROMOTIONAL_ACTIVITY_REVIEW", result.reasons)

    def test_conflicting_directions_preserved_but_review_required(self):
        negative = event(event_id="filing-2", category="FINANCING",
                         direction="NEGATIVE", source_url="https://sec.example.org/2")
        result = assess(event(), negative)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(result.classification, "REVIEW_REQUIRED")
        self.assertIn("CONFLICTING_EVENT_DIRECTIONS", result.reasons)

    def test_same_event_id_dedupes_exact_duplicates(self):
        self.assertEqual(len(assess(event(), event()).evidence), 1)

    def test_same_event_id_changed_payload_is_not_silently_overwritten(self):
        result = assess(event(), event(direction="NEGATIVE"))
        self.assertEqual(result.classification, "REVIEW_REQUIRED")
        self.assertIn("CONFLICTING_EVENT_ID", result.reasons)

    def test_conflicting_known_revision_before_close_requires_review(self):
        recent = event()
        old = replace(recent, published_at=CLOSE - timedelta(days=1),
                      first_seen_at=CLOSE - timedelta(days=1))
        for records in ((recent, old), (old, recent)):
            result = assess(*records)
            self.assertEqual(result.classification, "REVIEW_REQUIRED")
            self.assertIn("CONFLICTING_EVENT_ID", result.reasons)

    def test_future_revision_does_not_poison_known_event(self):
        future = replace(event(), direction="NEGATIVE",
                         received_at=NOW + timedelta(minutes=1))
        result = assess(event(), future)
        self.assertEqual(result.classification, "PRIMARY_EVENT")
        self.assertNotIn("CONFLICTING_EVENT_ID", result.reasons)

    def test_invalid_timestamps_do_not_qualify(self):
        result = assess(event(first_seen_at=datetime(2026, 11, 2, 23)))
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.classification, "REVIEW_REQUIRED")

    def test_untrusted_url_is_not_accepted(self):
        for url in ("http://example.org/a", "https://user:pass@x.org/a",
                    "https://x.org:1234/a", "file:///tmp/a", "https://x.org/a#fragment"):
            with self.subTest(url=url):
                self.assertEqual(assess(event(source_url=url)).classification,
                                 "REVIEW_REQUIRED")

    def test_malformed_identity_and_boolean_fail_closed(self):
        for altered in (event(symbol="OTHER"), event(source_origin_verified="true"),
                        event(event_id=""), event(promotional=1),
                        event(direction="MAYBE")):
            self.assertEqual(assess(altered).classification, "REVIEW_REQUIRED")

    def test_unhashable_metadata_types_fail_closed(self):
        for mutated in (event(category=[]), event(source_kind=[]), event(direction=[])):
            self.assertEqual(assess(mutated).classification, "REVIEW_REQUIRED")

    def test_received_exactly_at_decision_is_available(self):
        timely = event(received_at=NOW)
        self.assertEqual(assess(timely).classification, "PRIMARY_EVENT")

    def test_missing_events_do_not_infer_false_allegation(self):
        result = assess()
        self.assertEqual(result.classification, "NO_TIMELY_EVENT")
        self.assertNotIn("PROMOTIONAL_ACTIVITY_REVIEW", result.reasons)

    def test_stable_order_across_input_permutations(self):
        second = event(event_id="z", received_at=event().received_at + timedelta(seconds=1))
        self.assertEqual(assess(event(), second), assess(second, event()))

    def test_invalid_assessment_context_raises(self):
        with self.assertRaises(ValueError):
            assess_catalysts("TEST", as_of=CLOSE,
                              previous_session_close_at=CLOSE, events=[])
        with self.assertRaises(ValueError):
            assess_catalysts("test", as_of=NOW,
                              previous_session_close_at=CLOSE, events=[])


if __name__ == "__main__":
    unittest.main()
