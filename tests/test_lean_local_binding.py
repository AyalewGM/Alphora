"""Synthetic byte-binding tests; no LEAN, market feed, broker or network."""
from dataclasses import replace
from datetime import date
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alphora.research.lean_evidence import LeanBacktestReceipt
from alphora.research.lean_local_binding import (
    bind_local_lean_artifacts, LocalArtifactBinding,
)

FIELDS = (
    "algorithm_sha256", "data_sha256", "calendar_sha256",
    "corporate_actions_sha256", "license_receipt_sha256",
    "settings_sha256", "input_manifest_sha256", "output_sha256",
)


class LeanLocalBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = {field: f"artifact_{i}.bin" for i, field in enumerate(FIELDS)}
        hashes = {}
        for i, field in enumerate(FIELDS):
            # Tiny fabricated bytes, never licensed data or authorization files.
            raw = f"synthetic local evidence {i}\n".encode("ascii")
            (self.root / self.names[field]).write_bytes(raw)
            hashes[field] = sha256(raw).hexdigest()
        self.receipt = LeanBacktestReceipt(
            run_mode="backtest", benchmark_mode="strategy",
            start_session=date(2025, 1, 2), end_session=date(2025, 4, 1),
            cli_version="1.2.3",
            engine_image_digest="sha256:" + "a" * 64,
            **hashes,
        )

    def bind(self, **changes):
        args = dict(receipt=self.receipt, root=self.root, filenames=self.names)
        args.update(changes)
        return bind_local_lean_artifacts(**args)

    def test_local_bytes_match_receipt_repeatably_without_paths_in_result(self):
        result = self.bind()
        self.assertIsInstance(result, LocalArtifactBinding)
        self.assertEqual(result, self.bind())
        self.assertEqual(len(result.artifact_hashes), 8)
        self.assertEqual(len(result.binding_sha256), 64)
        self.assertEqual(dict(result.artifact_hashes)["data_sha256"],
                         self.receipt.data_sha256)
        self.assertNotIn(str(self.root), repr(result))
        self.assertFalse(hasattr(result, "place_order"))

    def test_changed_file_bytes_fail_closed_even_with_valid_receipt_digest(self):
        path = self.root / self.names["data_sha256"]
        path.write_bytes(b"altered synthetic market bytes")
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_HASH_MISMATCH"):
            self.bind()

    def test_valid_format_but_wrong_independent_pin_fails(self):
        changed = replace(self.receipt, output_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_HASH_MISMATCH"):
            self.bind(receipt=changed)

    def test_no_path_traversal_absolute_names_duplicate_or_missing_fields(self):
        for invalid in (
            {**self.names, "data_sha256": "../elsewhere"},
            {**self.names, "data_sha256": "/etc/passwd"},
            {**self.names, "data_sha256": "."},
            {**self.names, "data_sha256": ".."},
            {**self.names, "data_sha256": "artifacts/x"},
            {**self.names, "data_sha256": self.names["algorithm_sha256"]},
            {key: val for key, val in self.names.items() if key != "output_sha256"},
            [self.names],
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    ValueError, "INVALID_LOCAL_ARTIFACT_SET"):
                self.bind(filenames=invalid)

    def test_missing_file_symlink_and_hardlink_are_rejected(self):
        original = self.root / self.names["calendar_sha256"]
        original.unlink()
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_UNREADABLE"):
            self.bind()
        target = self.root / self.names["algorithm_sha256"]
        try:
            original.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this filesystem")
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_UNREADABLE"):
            self.bind()
        original.unlink()
        os.link(target, original)
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_NOT_REGULAR"):
            self.bind()

    def test_reject_non_file_inputs_and_empty_artifacts(self):
        output = self.root / self.names["output_sha256"]
        output.unlink()
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_NOT_REGULAR"):
            self.bind()
        output.rmdir()
        output.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_SIZE_UNSUPPORTED"):
            self.bind()

    def test_large_artifact_and_invalid_limit_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_SIZE_UNSUPPORTED"):
            self.bind(max_file_bytes=3)
        for bad in (0, -1, True, "1024", 4 * 1024 * 1024 * 1024 + 1):
            with self.subTest(limit=bad), self.assertRaisesRegex(
                    ValueError, "INVALID_LOCAL_ARTIFACT_LIMIT"):
                self.bind(max_file_bytes=bad)

    def test_untrusted_root_and_symlink_root_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "UNSAFE_LOCAL_ARTIFACT_ROOT"):
            self.bind(root="relative/evidence")
        other = self.root / "link_root"
        try:
            other.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this filesystem")
        with self.assertRaisesRegex(ValueError, "UNSAFE_LOCAL_ARTIFACT_ROOT"):
            self.bind(root=other)

    def test_live_or_paper_execution_mode_receipts_rejected(self):
        for mode in ("live", "paper"):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                    ValueError, "UNSAFE_LEAN_RUN_MODE"):
                self.bind(receipt=replace(self.receipt, run_mode=mode))

    def test_directory_entry_mutation_mid_binding_is_rejected(self):
        # Even when all eight files and their bytes remain unchanged,
        # an entry created by another writer between reads invalidates
        # the claim that they were observed under a stable directory.
        import alphora.research.lean_local_binding as module
        underlying = module._hash_named_local_file
        calls = [0]

        def hash_and_change_root(fd, filename, *, max_file_bytes):
            result = underlying(fd, filename, max_file_bytes=max_file_bytes)
            calls[0] += 1
            if calls[0] == 1:
                (self.root / "new_unrelated_entry.tmp").write_bytes(b"race")
                st = self.root.stat()
                # Force a time change even on low-resolution filesystems.
                os.utime(self.root, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
            return result

        with patch.object(module, "_hash_named_local_file", side_effect=hash_and_change_root):
            with self.assertRaisesRegex(ValueError, "LOCAL_ARTIFACT_DIRECTORY_CHANGED"):
                self.bind()
        self.assertEqual(calls[0], 8)
        self.assertIsInstance(self.bind(), LocalArtifactBinding)

    def test_different_filenames_rebind_bytes_but_change_binding_identity(self):
        before = self.bind()
        old = self.root / self.names["settings_sha256"]
        renamed = self.root / "settings_evidence.bin"
        old.rename(renamed)
        names = {**self.names, "settings_sha256": renamed.name}
        after = self.bind(filenames=names)
        self.assertEqual(before.artifact_hashes, after.artifact_hashes)
        self.assertEqual(before.receipt_sha256, after.receipt_sha256)
        self.assertNotEqual(before.binding_sha256, after.binding_sha256)


if __name__ == "__main__":
    unittest.main()
