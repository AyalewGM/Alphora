"""Bind both three-leg LEAN research suites to local artifact bytes, offline only.

This is an integrity/repeatability *preflight*, not evidence that LEAN ran,
that independently licensed market data were used, or that outputs are
financially accurate. Never launches an engine, downloads data or sends orders.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from .lean_evidence import LeanBacktestReceipt, verify_repeated_suite
from .lean_local_binding import (
    LocalArtifactBinding, MAX_FILE_BYTES, bind_local_lean_artifacts,
)

_MODES = ("spy-10pct-and-cash", "spy-fully-invested-total-return", "strategy")


@dataclass(frozen=True)
class RepeatedLocalSuiteBinding:
    """Portable fingerprints of two caller-attested 3-leg backtest attempts."""
    receipts: tuple[tuple[str, str], ...]
    first_attempt: tuple[tuple[str, str], ...]
    second_attempt: tuple[tuple[str, str], ...]
    binding_sha256: str


def _root_identity(root) -> tuple[int, int]:
    """Reject alias directories, including separate paths to the same inode."""
    if (type(root) is not str and type(root) is not Path
            or not Path(root).is_absolute()
            or not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_DIRECTORY", 0)
            or not getattr(os, "O_CLOEXEC", 0)):
        raise ValueError("INVALID_SUITE_ARTIFACT_ROOT")
    try:
        fd = os.open(os.fspath(root), os.O_RDONLY | os.O_DIRECTORY
                     | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISDIR(meta.st_mode):
                raise ValueError("INVALID_SUITE_ARTIFACT_ROOT")
            return meta.st_dev, meta.st_ino
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError("INVALID_SUITE_ARTIFACT_ROOT") from exc


def bind_repeated_local_lean_suites(
    first_attempt: tuple[LeanBacktestReceipt, ...],
    second_attempt: tuple[LeanBacktestReceipt, ...],
    *,
    roots: tuple[tuple[str | Path, ...], tuple[str | Path, ...]],
    filenames: tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...]],
    max_file_bytes: int = MAX_FILE_BYTES,
) -> RepeatedLocalSuiteBinding:
    """Verify metadata and *six distinct* local 8-file artifact directories.

    Each roots/filenames tuple is positional with its corresponding receipt
    tuple, while the returned result is normalized by benchmark mode. Different
    inodes limit accidental reuse of one run as six runs; identical copied
    artifacts or fabricated receipts can still pass. This cannot attest
    independent execution, license rights or reproducible numerical results.
    """
    # Refuse mismatched modes, epochs, engine digest, attempts or outputs
    # BEFORE reading any local file, to avoid partial success claims.
    fingerprints = verify_repeated_suite(first_attempt, second_attempt)
    if (type(roots) is not tuple or len(roots) != 2
            or type(filenames) is not tuple or len(filenames) != 2
            or any(type(group) is not tuple or len(group) != len(_MODES)
                   for group in (*roots, *filenames))
            or type(max_file_bytes) is not int
            or not 0 < max_file_bytes <= MAX_FILE_BYTES):
        raise ValueError("INVALID_REPEATED_LOCAL_SUITE")

    # Unique directory identity is checked up front. These aren't proofs
    # of atomic six-directory snapshots (e.g. rename races may still exist).
    identities = []
    for group in roots:
        for root in group:
            identities.append(_root_identity(root))
    if len(set(identities)) != len(identities):
        raise ValueError("REUSED_SUITE_ARTIFACT_DIRECTORY")

    attempts = []
    for receipts, dirs, names in zip(
        (first_attempt, second_attempt), roots, filenames
    ):
        by_mode = {}
        for receipt, root, mapping in zip(receipts, dirs, names):
            binding: LocalArtifactBinding = bind_local_lean_artifacts(
                receipt, root=root, filenames=mapping,
                max_file_bytes=max_file_bytes,
            )
            by_mode[receipt.benchmark_mode] = binding.binding_sha256
        attempts.append(tuple((mode, by_mode[mode]) for mode in _MODES))

    canonical = {
        "schema": 1,
        "receipts": tuple(sorted(fingerprints.items())),
        "first_attempt": attempts[0],
        "second_attempt": attempts[1],
    }
    digest = sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return RepeatedLocalSuiteBinding(
        receipts=canonical["receipts"],
        first_attempt=attempts[0], second_attempt=attempts[1],
        binding_sha256=digest,
    )
