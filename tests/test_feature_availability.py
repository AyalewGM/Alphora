"""Synthetic-only feature chronology tests; zero market or broker access."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from alphora.research.feature_availability import (
    MAX_FEATURE_INPUTS, FeatureEvidence, ResearchFeatureDecision,
    audit_feature_availability,
)

UTC = timezone.utc
T = datetime(2026, 1, 5, 16, tzinfo=UTC)
H1, H2 = "1" * 64, "2" * 64


def fixture():
    price = FeatureEvidence("price.1", "PRICE", T, T + timedelta(seconds=1),
                            T + timedelta(seconds=2), H1)
    news = FeatureEvidence("news.1", "NEWS", T, T + timedelta(seconds=2),
                           T + timedelta(seconds=3), H2)
    return ResearchFeatureDecision("asset.SPY", T + timedelta(seconds=10),
                                   (price, news))


class FeatureAvailabilityTests(unittest.TestCase):
    def test_valid_order_and_timezone_equivalence_are_deterministic(self):
        d = fixture()
        receipt = audit_feature_availability(
            d, expected_artifact_sha256={"price.1": H1, "news.1": H2})
        self.assertEqual((receipt.feature_count, receipt.news_feature_count), (2, 1))
        self.assertEqual(receipt.provenance, "CALLER_ATTESTED_ONLY")
        reordered = replace(d, inputs=tuple(reversed(d.inputs)))
        self.assertEqual(receipt, audit_feature_availability(
            reordered, expected_artifact_sha256={"news.1": H2, "price.1": H1}))
        later_timezone = timezone(timedelta(hours=-5))
        self.assertEqual(receipt, audit_feature_availability(
            replace(d, decision_at=d.decision_at.astimezone(later_timezone)),
            expected_artifact_sha256={"news.1": H2, "price.1": H1},
            expected_receipt_sha256=receipt.sha256))

    def test_rejects_future_measurement_and_first_receipt(self):
        d = fixture()
        for mutation in ("measured_through_at", "first_seen_at", "version_available_at"):
            with self.subTest(mutation=mutation):
                replacement = replace(d.inputs[0], **{mutation: d.decision_at + timedelta(seconds=1)})
                with self.assertRaisesRegex(ValueError, "FEATURE_NOT_AVAILABLE_AT_DECISION"):
                    audit_feature_availability(replace(d, inputs=(replacement, d.inputs[1])),
                                               expected_artifact_sha256={"price.1": H1, "news.1": H2})

    def test_strict_decision_time_boundary_and_chronology(self):
        d = fixture()
        cases = [replace(d.inputs[0], version_available_at=d.decision_at),
                 replace(d.inputs[0], first_seen_at=d.inputs[0].version_available_at + timedelta(seconds=1)),
                 replace(d.inputs[0], measured_through_at=d.inputs[0].first_seen_at + timedelta(seconds=1))]
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "FEATURE_NOT_AVAILABLE_AT_DECISION"):
                    audit_feature_availability(replace(d, inputs=(changed, d.inputs[1])),
                                               expected_artifact_sha256={"price.1": H1, "news.1": H2})

    def test_missing_mismatched_and_extra_pins_are_rejected(self):
        d = fixture()
        for pins in ({}, {"price.1": H1, "news.1": H1},
                     {"price.1": H1, "news.1": H2, "extra": H1},
                     {"price.1": H1, "other": H2}):
            with self.subTest(pins=pins):
                with self.assertRaisesRegex(ValueError, "INVALID_FEATURE_PINS|FEATURE_PIN_MISMATCH"):
                    audit_feature_availability(d, expected_artifact_sha256=pins)

    def test_replay_receipt_pin_rejects_changed_revision_or_timestamp(self):
        d = fixture()
        pins = {"price.1": H1, "news.1": H2}
        receipt = audit_feature_availability(d, expected_artifact_sha256=pins)
        altered = replace(d.inputs[0], first_seen_at=d.inputs[0].first_seen_at + timedelta(milliseconds=1))
        with self.assertRaisesRegex(ValueError, "FEATURE_RECEIPT_MISMATCH"):
            audit_feature_availability(replace(d, inputs=(altered, d.inputs[1])),
                                       expected_artifact_sha256=pins,
                                       expected_receipt_sha256=receipt.sha256)
        changed = replace(d.inputs[0], artifact_sha256=H2)
        with self.assertRaisesRegex(ValueError, "FEATURE_PIN_MISMATCH"):
            audit_feature_availability(replace(d, inputs=(changed, d.inputs[1])),
                                       expected_artifact_sha256=pins)

    def test_duplicate_and_invalid_kind_fail_closed(self):
        d = fixture()
        for inputs in ((d.inputs[0], d.inputs[0]),
                       (replace(d.inputs[0], kind="FAKE_NEWS"), d.inputs[1])):
            with self.assertRaisesRegex(ValueError, "INVALID_FEATURE_EVIDENCE"):
                audit_feature_availability(replace(d, inputs=inputs),
                                           expected_artifact_sha256={"price.1": H1, "news.1": H2})

    def test_invalid_decision_types_and_timezones(self):
        d = fixture()
        for x in (replace(d, decision_at=d.decision_at.replace(tzinfo=None)),
                  replace(d, asset_id="bad asset"),
                  replace(d, inputs=list(d.inputs)),
                  replace(d, inputs=()),
                  replace(d, inputs=(d.inputs[0],) * (MAX_FEATURE_INPUTS + 1))):
            with self.subTest(x=repr(x)[:80]):
                with self.assertRaisesRegex(ValueError, "INVALID_RESEARCH_DECISION"):
                    audit_feature_availability(x, expected_artifact_sha256={})

    def test_naive_feature_timestamp_and_invalid_hash(self):
        d = fixture()
        for changed in (replace(d.inputs[0], first_seen_at=T.replace(tzinfo=None)),
                        replace(d.inputs[0], artifact_sha256="not-a-hash"),
                        replace(d.inputs[0], input_id="unsafe id")):
            with self.assertRaisesRegex(ValueError, "INVALID_FEATURE_EVIDENCE"):
                audit_feature_availability(replace(d, inputs=(changed, d.inputs[1])),
                                           expected_artifact_sha256={"price.1": H1, "news.1": H2})

    def test_invalid_receipt_pin_and_external_pin_type(self):
        d = fixture()
        with self.assertRaisesRegex(ValueError, "INVALID_FEATURE_PINS"):
            audit_feature_availability(d, expected_artifact_sha256={"price.1": H1, "news.1": H2},
                                       expected_receipt_sha256="invalid")
        with self.assertRaisesRegex(ValueError, "INVALID_FEATURE_PINS"):
            audit_feature_availability(d, expected_artifact_sha256=[H1, H2])


if __name__ == "__main__":
    unittest.main()
