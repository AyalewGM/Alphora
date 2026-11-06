"""Synthetic-only tests. No LEAN CLI, datasets, credentials, network or orders."""

from dataclasses import replace
from datetime import date, datetime
import unittest

from alphora.research.lean_evidence import (
    LeanBacktestReceipt, receipt_fingerprint, validate_receipt, verify_repeat,
)


class LeanEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.r = LeanBacktestReceipt(
            run_mode="backtest", benchmark_mode="spy-10pct-and-cash",
            start_session=date(2025, 1, 2), end_session=date(2025, 4, 1),
            cli_version="1.2.3", engine_image_digest="sha256:" + "a" * 64,
            algorithm_sha256="b" * 64, data_sha256="c" * 64,
            calendar_sha256="d" * 64, corporate_actions_sha256="e" * 64,
            license_receipt_sha256="f" * 64, settings_sha256="0" * 64,
            input_manifest_sha256="1" * 64, output_sha256="2" * 64,
        )

    def test_receipt_and_repeated_identity(self):
        validate_receipt(self.r)
        fingerprint = receipt_fingerprint(self.r)
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(fingerprint, receipt_fingerprint(replace(self.r)))
        self.assertEqual(verify_repeat(self.r, replace(self.r)), fingerprint)

    def test_never_accept_live_or_paper_execution_as_backtest(self):
        for mode in ("live", "paper", "lean live", "BACKTEST", "", None):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "UNSAFE_LEAN_RUN_MODE"):
                validate_receipt(replace(self.r, run_mode=mode))

    def test_benchmark_exposure_modes_are_explicit(self):
        for benchmark in ("strategy", "spy-fully-invested-total-return", "spy-10pct-and-cash"):
            validate_receipt(replace(self.r, benchmark_mode=benchmark))
        for benchmark in ("SPY", "spy-total-return", None, 7, [], {}):
            with self.subTest(benchmark=benchmark), self.assertRaisesRegex(ValueError, "INVALID_BENCHMARK_MODE"):
                validate_receipt(replace(self.r, benchmark_mode=benchmark))

    def test_calendar_dates_must_be_strict(self):
        for start, end in ((date(2025, 4, 1), date(2025, 4, 1)),
                           (date(2025, 5, 1), date(2025, 4, 1)),
                           (datetime(2025, 1, 1), date(2025, 4, 1)),
                           ("2025-01-02", date(2025, 4, 1))):
            with self.subTest(start=start), self.assertRaisesRegex(ValueError, "INVALID_EVALUATION_DATES"):
                validate_receipt(replace(self.r, start_session=start, end_session=end))

    def test_digests_are_pinned_64_hex_not_claims(self):
        for bad in ("abc", "A" * 64, "g" * 64, 5, "", None):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "INVALID_EVIDENCE_DIGEST"):
                validate_receipt(replace(self.r, data_sha256=bad))
        for bad in ("a" * 64, "sha256:" + "A" * 64, "sha512:" + "a" * 64):
            with self.subTest(image=bad), self.assertRaisesRegex(ValueError, "INVALID_ENGINE_DIGEST"):
                validate_receipt(replace(self.r, engine_image_digest=bad))

    def test_reject_embedded_commands_or_secrets_in_version(self):
        for bad in ("lean backtest", "1.0; lean live", "--paper", "", None, "1.2\nKEY=x"):
            with self.subTest(version=bad), self.assertRaisesRegex(ValueError, "INVALID_LEAN_CLI_VERSION"):
                validate_receipt(replace(self.r, cli_version=bad))

    def test_repeat_rejects_changed_outputs(self):
        with self.assertRaisesRegex(ValueError, "LEAN_RUN_OUTPUT_MISMATCH"):
            verify_repeat(self.r, replace(self.r, output_sha256="9" * 64))

    def test_repeat_rejects_changed_input_and_exposure(self):
        for field, value in (("algorithm_sha256", "8" * 64),
                             ("data_sha256", "8" * 64),
                             ("settings_sha256", "8" * 64),
                             ("benchmark_mode", "spy-fully-invested-total-return"),
                             ("end_session", date(2025, 4, 2))):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "LEAN_RUN_INPUT_MISMATCH"):
                verify_repeat(self.r, replace(self.r, **{field: value}))

    def test_untyped_receipts_rejected(self):
        for other in (None, {}, "fake", object()):
            with self.subTest(other=type(other)), self.assertRaisesRegex(ValueError, "INVALID_LEAN_RECEIPT"):
                verify_repeat(self.r, other)


if __name__ == "__main__":
    unittest.main()
