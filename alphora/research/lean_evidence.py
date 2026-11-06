"""Pure, offline LEAN backtest evidence checks; never invokes LEAN or a broker.

An evidence receipt is caller-supplied metadata, NOT proof that an engine ran,
that data were licensed, or that an output is financially correct.
"""

from dataclasses import dataclass, fields
from datetime import date
from hashlib import sha256
import json
import re

_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_CLI = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z", re.ASCII)
_MODES = frozenset({"strategy", "spy-fully-invested-total-return", "spy-10pct-and-cash"})
_INPUT_FIELDS = (
    "run_mode", "benchmark_mode", "start_session", "end_session", "cli_version",
    "engine_image_digest", "algorithm_sha256", "data_sha256",
    "calendar_sha256", "corporate_actions_sha256", "license_receipt_sha256",
    "settings_sha256", "input_manifest_sha256",
)
_HASH_FIELDS = (
    "algorithm_sha256", "data_sha256", "calendar_sha256",
    "corporate_actions_sha256", "license_receipt_sha256", "settings_sha256",
    "input_manifest_sha256", "output_sha256",
)


@dataclass(frozen=True)
class LeanBacktestReceipt:
    """Caller-attested identity for ONE isolated offline backtest attempt."""

    run_mode: str
    benchmark_mode: str
    start_session: date
    end_session: date
    cli_version: str
    engine_image_digest: str
    algorithm_sha256: str
    data_sha256: str
    calendar_sha256: str
    corporate_actions_sha256: str
    license_receipt_sha256: str
    settings_sha256: str
    input_manifest_sha256: str
    output_sha256: str


def validate_receipt(receipt: LeanBacktestReceipt) -> None:
    """Reject malformed/unsafe caller attestations without accessing files."""
    if type(receipt) is not LeanBacktestReceipt:
        raise ValueError("INVALID_LEAN_RECEIPT")
    if receipt.run_mode != "backtest":
        raise ValueError("UNSAFE_LEAN_RUN_MODE")
    if type(receipt.benchmark_mode) is not str or receipt.benchmark_mode not in _MODES:
        raise ValueError("INVALID_BENCHMARK_MODE")
    if (type(receipt.start_session) is not date or type(receipt.end_session) is not date
            or receipt.start_session >= receipt.end_session):
        raise ValueError("INVALID_EVALUATION_DATES")
    if type(receipt.cli_version) is not str or not _CLI.fullmatch(receipt.cli_version):
        raise ValueError("INVALID_LEAN_CLI_VERSION")
    if (type(receipt.engine_image_digest) is not str
            or not _IMAGE.fullmatch(receipt.engine_image_digest)):
        raise ValueError("INVALID_ENGINE_DIGEST")
    if any(type(getattr(receipt, name)) is not str
           or not _HASH.fullmatch(getattr(receipt, name)) for name in _HASH_FIELDS):
        raise ValueError("INVALID_EVIDENCE_DIGEST")


def receipt_fingerprint(receipt: LeanBacktestReceipt) -> str:
    """Canonical identity hash of attested metadata; NOT an authentication seal."""
    validate_receipt(receipt)
    canonical = {field.name: getattr(receipt, field.name) for field in fields(receipt)}
    canonical["start_session"] = receipt.start_session.isoformat()
    canonical["end_session"] = receipt.end_session.isoformat()
    content = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(content.encode("utf-8")).hexdigest()


def verify_repeat(first: LeanBacktestReceipt, second: LeanBacktestReceipt) -> str:
    """Check identical recorded inputs AND output hashes for two local runs.

    Caller must independently bind these digests to actual external artifacts,
    verify local LEAN execution and reconcile numerical outcomes. Copying a
    receipt twice will pass this check and does not prove repeat execution.
    """
    validate_receipt(first)
    validate_receipt(second)
    if any(getattr(first, name) != getattr(second, name) for name in _INPUT_FIELDS):
        raise ValueError("LEAN_RUN_INPUT_MISMATCH")
    if first.output_sha256 != second.output_sha256:
        raise ValueError("LEAN_RUN_OUTPUT_MISMATCH")
    return receipt_fingerprint(first)


_SUITE_ALIGNMENT_FIELDS = (
    "start_session", "end_session", "calendar_sha256",
    "cli_version", "engine_image_digest",
)


def verify_suite(receipts: tuple[LeanBacktestReceipt, ...]) -> dict[str, str]:
    """Check attested dates, calendar and engine identity across all three legs.

    This is a metadata-only preflight, NOT verification that the legs actually
    ran on identical sessions, market data, or financial accounting conventions.
    Different strategy/benchmark inputs may legitimately have different hashes.
    """
    if type(receipts) is not tuple or len(receipts) != len(_MODES):
        raise ValueError("INCOMPLETE_LEAN_BENCHMARK_SUITE")
    by_mode = {}
    for receipt in receipts:
        validate_receipt(receipt)
        if receipt.benchmark_mode in by_mode:
            raise ValueError("INCOMPLETE_LEAN_BENCHMARK_SUITE")
        by_mode[receipt.benchmark_mode] = receipt
    if set(by_mode) != _MODES:
        raise ValueError("INCOMPLETE_LEAN_BENCHMARK_SUITE")
    reference = by_mode["strategy"]
    if any(getattr(receipt, field) != getattr(reference, field)
           for receipt in by_mode.values() for field in _SUITE_ALIGNMENT_FIELDS):
        raise ValueError("LEAN_BENCHMARK_METADATA_MISMATCH")
    return {mode: receipt_fingerprint(by_mode[mode]) for mode in sorted(_MODES)}
