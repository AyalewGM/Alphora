"""Two independent-directory LEAN-suite byte-binding, synthetic-only fixtures."""
from dataclasses import replace
from datetime import date
from hashlib import sha256
import tempfile
from pathlib import Path
import unittest

from alphora.research.lean_evidence import LeanBacktestReceipt
from alphora.research.lean_suite_binding import bind_repeated_local_lean_suites

MODES = ("strategy", "spy-fully-invested-total-return", "spy-10pct-and-cash")
FIELDS = (
    "algorithm_sha256", "data_sha256", "calendar_sha256",
    "corporate_actions_sha256", "license_receipt_sha256",
    "settings_sha256", "input_manifest_sha256", "output_sha256",
)


class RepeatedLocalSuiteBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        receipts, roots, names = [], [], []
        for attempt in range(2):
            r_group, path_group, name_group = [], [], []
            for mode in MODES:
                root = self.base / f"attempt{attempt}-{mode}"
                root.mkdir()
                fields = {}
                mapping = {}
                for field in FIELDS:
                    # Same bytes for the repeated run's same mode, while
                    # distinct benchmark modes have independent outputs.
                    blob = ("calendar" if field == "calendar_sha256"
                            else f"{mode}/{field}").encode()
                    name = field.replace("_sha256", "") + ".dat"
                    (root / name).write_bytes(blob)
                    mapping[field] = name
                    fields[field] = sha256(blob).hexdigest()
                receipt = LeanBacktestReceipt(
                    run_mode="backtest", benchmark_mode=mode,
                    start_session=date(2025, 1, 2),
                    end_session=date(2025, 4, 1),
                    cli_version="1.2.3",
                    engine_image_digest="sha256:" + "a" * 64,
                    **fields,
                )
                r_group.append(receipt)
                path_group.append(root)
                name_group.append(mapping)
            receipts.append(tuple(r_group))
            roots.append(tuple(path_group))
            names.append(tuple(name_group))
        self.first, self.second = receipts
        self.roots, self.names = tuple(roots), tuple(names)

    def bind(self, **options):
        args = {
            "roots": self.roots, "filenames": self.names,
        }
        args.update(options)
        return bind_repeated_local_lean_suites(
            self.first, self.second, **args,
        )

    def test_all_six_distinct_directory_attestations_have_stable_digest(self):
        result = self.bind()
        self.assertEqual(len(result.receipts), 3)
        self.assertEqual(len(result.first_attempt), 3)
        self.assertEqual(len(result.second_attempt), 3)
        self.assertEqual(set(dict(result.receipts)), set(MODES))
        self.assertEqual(len(result.binding_sha256), 64)
        self.assertEqual(self.bind(), result)
        self.assertNotIn(str(self.base), str(result))

    def test_receipt_order_does_not_change_canonical_fingerprint(self):
        perm = (2, 0, 1)
        first = tuple(self.first[i] for i in perm)
        second = tuple(self.second[i] for i in perm)
        roots = tuple(tuple(group[i] for i in perm) for group in self.roots)
        names = tuple(tuple(group[i] for i in perm) for group in self.names)
        self.assertEqual(
            self.bind(),
            bind_repeated_local_lean_suites(
                first, second, roots=roots, filenames=names,
            ),
        )

    def test_reusing_single_artifact_root_is_rejected_even_when_hashes_match(self):
        duplicate = (
            self.roots[0],
            (self.roots[0][0], *self.roots[1][1:]),
        )
        with self.assertRaisesRegex(ValueError, "REUSED_SUITE_ARTIFACT_DIRECTORY"):
            self.bind(roots=duplicate)

    def test_directory_alias_is_rejected(self):
        alias = self.base / "duplicate-alias"
        alias.symlink_to(self.roots[0][0], target_is_directory=True)
        duplicate = (
            self.roots[0],
            (alias, *self.roots[1][1:]),
        )
        with self.assertRaisesRegex(ValueError, "INVALID_SUITE_ARTIFACT_ROOT"):
            self.bind(roots=duplicate)

    def test_same_receipt_with_tampered_local_bytes_is_rejected(self):
        root = self.roots[1][1]
        (root / self.names[1][1]["output_sha256"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_HASH_MISMATCH"):
            self.bind()

    def test_mismatched_repeated_metadata_fails_before_file_reads(self):
        changed = list(self.second)
        changed[0] = replace(changed[0], output_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "LEAN_RUN_OUTPUT_MISMATCH"):
            bind_repeated_local_lean_suites(
                self.first, tuple(changed),
                roots=self.roots, filenames=self.names,
            )

    def test_incomplete_or_invalid_shapes_fail_closed(self):
        for bad in (
            {"roots": self.roots[:1]},
            {"filenames": self.names[:1]},
            {"roots": (list(self.roots[0]), self.roots[1])},
            {"max_file_bytes": 0},
            {"max_file_bytes": True},
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                ValueError, "INVALID_REPEATED_LOCAL_SUITE"
            ):
                self.bind(**bad)

    def test_non_backtest_receipt_is_forbidden(self):
        wrong = list(self.first)
        wrong[0] = replace(wrong[0], run_mode="live")
        with self.assertRaisesRegex(ValueError, "UNSAFE_LEAN_RUN_MODE"):
            bind_repeated_local_lean_suites(
                tuple(wrong), self.second,
                roots=self.roots, filenames=self.names,
            )


if __name__ == "__main__":
    unittest.main()
