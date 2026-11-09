"""Off-repository, read-only SHA-256 binding for caller-attested LEAN receipts.

No network access, broker, LEAN invocation, orders, credential handling, file
writes, authentication or financial accounting. This utility verifies bytes
of a small, explicit set of LOCAL files against hashes claimed by a receipt.
It cannot authenticate file origin, licenses, run independence or engine truth.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from .lean_evidence import LeanBacktestReceipt, receipt_fingerprint, validate_receipt

_ARTIFACT_FIELDS = (
    "algorithm_sha256",
    "data_sha256",
    "calendar_sha256",
    "corporate_actions_sha256",
    "license_receipt_sha256",
    "settings_sha256",
    "input_manifest_sha256",
    "output_sha256",
)
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024  # bounded local copy, not a recommended market data size
_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LocalArtifactBinding:
    receipt_sha256: str
    artifact_hashes: tuple[tuple[str, str], ...]
    binding_sha256: str  # locally computed fingerprint, NOT an authenticated signature


def _hash_named_local_file(dir_fd: int, filename: str, *, max_file_bytes: int) -> str:
    """Read one flat filename through a trusted directory handle, never a link."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(filename, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ValueError("LOCAL_ARTIFACT_UNREADABLE") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("LOCAL_ARTIFACT_NOT_REGULAR")
        if before.st_size <= 0 or before.st_size > max_file_bytes:
            raise ValueError("LOCAL_ARTIFACT_SIZE_UNSUPPORTED")
        digest = sha256()
        total = 0
        while True:
            data = os.read(fd, _CHUNK_BYTES)
            if not data:
                break
            total += len(data)
            if total > max_file_bytes:
                raise ValueError("LOCAL_ARTIFACT_SIZE_UNSUPPORTED")
            digest.update(data)
        after = os.fstat(fd)
        # Reject concurrent edits visible in metadata, even if the final
        # bytes would otherwise match an earlier externally supplied digest.
        before_identity = (before.st_dev, before.st_ino, before.st_size,
                           before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size,
                          after.st_mtime_ns, after.st_ctime_ns)
        if total != before.st_size or before_identity != after_identity:
            raise ValueError("LOCAL_ARTIFACT_CHANGED_DURING_READ")
        return digest.hexdigest()
    except OSError as exc:
        raise ValueError("LOCAL_ARTIFACT_UNREADABLE") from exc
    finally:
        os.close(fd)


def bind_local_lean_artifacts(
    receipt: LeanBacktestReceipt, *, root: str | Path,
    filenames: dict[str, str], max_file_bytes: int = MAX_FILE_BYTES,
) -> LocalArtifactBinding:
    """Compare eight local artifact files with a single LEAN evidence receipt.

    root must be an independently selected, absolute, trusted local directory.
    Filenames are intentionally flat; no paths, symlinks, hardlinks, devices,
    FIFOs or duplicate filenames are accepted. This does NOT prove LEAN ran.
    """
    validate_receipt(receipt)
    if type(max_file_bytes) is not int or not 0 < max_file_bytes <= MAX_FILE_BYTES:
        raise ValueError("INVALID_LOCAL_ARTIFACT_LIMIT")
    if (type(filenames) is not dict or set(filenames) != set(_ARTIFACT_FIELDS)
            or any(type(n) is not str or n in (".", "..")
                   or _FILENAME.fullmatch(n) is None
                   for n in filenames.values())
            or len(set(filenames.values())) != len(_ARTIFACT_FIELDS)):
        raise ValueError("INVALID_LOCAL_ARTIFACT_SET")
    if (not isinstance(root, (str, Path)) or
            not Path(root).is_absolute() or not str(root)):
        raise ValueError("UNSAFE_LOCAL_ARTIFACT_ROOT")
    # Reject platforms unable to enforce no-follow directory descriptors.
    if (not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_DIRECTORY", 0)
            or not getattr(os, "O_CLOEXEC", 0)):
        raise ValueError("UNSUPPORTED_LOCAL_FILE_BOUNDARY")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        dir_fd = os.open(os.fspath(root), flags)
    except OSError as exc:
        raise ValueError("UNSAFE_LOCAL_ARTIFACT_ROOT") from exc
    try:
        # A directory-handle pins the root inode, but does not by itself
        # detect entry replacement between individually opened artifacts.
        # A conservative directory metadata check rejects a changed set
        # during this multi-file read. It is not an atomic filesystem snapshot.
        root_before = os.fstat(dir_fd)
        observed = []
        for field in _ARTIFACT_FIELDS:
            actual_hash = _hash_named_local_file(
                dir_fd, filenames[field], max_file_bytes=max_file_bytes,
            )
            if actual_hash != getattr(receipt, field):
                raise ValueError("LOCAL_ARTIFACT_HASH_MISMATCH")
            observed.append((field, actual_hash))
        root_after = os.fstat(dir_fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)
        if identity(root_before) != identity(root_after):
            raise ValueError("LOCAL_ARTIFACT_DIRECTORY_CHANGED")
    except OSError as exc:
        raise ValueError("UNSAFE_LOCAL_ARTIFACT_ROOT") from exc
    finally:
        os.close(dir_fd)
    canonical = {
        "schema": 1,
        "receipt_sha256": receipt_fingerprint(receipt),
        "artifacts": [
            {"field": field, "name": filenames[field], "sha256": digest}
            for field, digest in observed
        ],
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")
    return LocalArtifactBinding(
        receipt_sha256=canonical["receipt_sha256"],
        artifact_hashes=tuple(observed),
        binding_sha256=sha256(data).hexdigest(),
    )
