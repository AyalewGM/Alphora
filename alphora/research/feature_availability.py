"""Bounded, offline evidence-timing guard for research feature inputs.

This checks only caller-supplied chronology and independently supplied hash
pins. It cannot authenticate vendors, prevent selection bias or authorize trades.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = frozenset({"PRICE", "NEWS"})
MAX_FEATURE_INPUTS = 128


def _aware(value: object) -> bool:
    return (type(value) is datetime and value.tzinfo is not None
            and value.utcoffset() is not None)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class FeatureEvidence:
    input_id: str  # Unique within one decision; NOT an authenticated provider ID.
    asset_id: str  # Claimed target asset for this derived feature; NOT authenticated.
    kind: str  # PRICE or NEWS; not an automatic eligibility classification.
    measured_through_at: datetime  # End of the information collection window.
    first_seen_at: datetime  # Claimed first receipt, not merely publication time.
    version_available_at: datetime  # Claimed availability of this exact revision.
    artifact_sha256: str  # Hash of exact source/version; not proof of its origin.


@dataclass(frozen=True)
class ResearchFeatureDecision:
    asset_id: str
    decision_at: datetime
    inputs: tuple[FeatureEvidence, ...]


@dataclass(frozen=True)
class FeatureAvailabilityReceipt:
    sha256: str  # Deterministic LOCAL fingerprint, not vendor authentication.
    feature_count: int
    news_feature_count: int
    provenance: str = "CALLER_ATTESTED_ONLY"


def audit_feature_availability(
    decision: ResearchFeatureDecision, *,
    expected_artifact_sha256: dict[str, str],
    expected_receipt_sha256: str | None = None,
) -> FeatureAvailabilityReceipt:
    """Reject lookahead and cross-asset evidence substitution in declared inputs.

    The caller supplies an independently recorded hash for every exact feature
    artifact. A matching pin detects inconsistent claims, not falsified data.
    Feature values, labels, fold membership and market session validity are
    outside this function. No network, broker, modeling, or order operations.
    """
    if (type(decision) is not ResearchFeatureDecision
            or type(decision.asset_id) is not str
            or _ID.fullmatch(decision.asset_id) is None
            or not _aware(decision.decision_at)
            or type(decision.inputs) is not tuple
            or not 1 <= len(decision.inputs) <= MAX_FEATURE_INPUTS):
        raise ValueError("INVALID_RESEARCH_DECISION")
    if (type(expected_artifact_sha256) is not dict
            or len(expected_artifact_sha256) != len(decision.inputs)
            or (expected_receipt_sha256 is not None and
                (type(expected_receipt_sha256) is not str or
                 _SHA.fullmatch(expected_receipt_sha256) is None))):
        raise ValueError("INVALID_FEATURE_PINS")

    rows = []
    ids = set()
    news = 0
    for item in decision.inputs:
        if (type(item) is not FeatureEvidence
                or type(item.input_id) is not str
                or _ID.fullmatch(item.input_id) is None
                or type(item.asset_id) is not str
                or _ID.fullmatch(item.asset_id) is None
                or item.input_id in ids
                or type(item.kind) is not str or item.kind not in _KINDS
                or not all(_aware(t) for t in (
                    item.measured_through_at, item.first_seen_at,
                    item.version_available_at))
                or type(item.artifact_sha256) is not str
                or _SHA.fullmatch(item.artifact_sha256) is None):
            raise ValueError("INVALID_FEATURE_EVIDENCE")
        if item.asset_id != decision.asset_id:
            raise ValueError("FEATURE_ASSET_MISMATCH")
        ids.add(item.input_id)
        pinned = expected_artifact_sha256.get(item.input_id)
        if (type(pinned) is not str or _SHA.fullmatch(pinned) is None
                or pinned != item.artifact_sha256):
            raise ValueError("FEATURE_PIN_MISMATCH")
        # Normalize BEFORE ordering. Datetimes sharing one ZoneInfo instance
        # compare by wall time across fall-back DST folds in Python, which can
        # otherwise accept future revisions or reject valid chronology.
        if not (item.measured_through_at.astimezone(timezone.utc)
                <= item.first_seen_at.astimezone(timezone.utc)
                <= item.version_available_at.astimezone(timezone.utc)
                < decision.decision_at.astimezone(timezone.utc)):
            raise ValueError("FEATURE_NOT_AVAILABLE_AT_DECISION")
        news += item.kind == "NEWS"
        rows.append({
            "id": item.input_id,
            "asset_id": item.asset_id,
            "kind": item.kind,
            "measured_through_at": _utc(item.measured_through_at),
            "first_seen_at": _utc(item.first_seen_at),
            "version_available_at": _utc(item.version_available_at),
            "artifact_sha256": item.artifact_sha256,
        })
    if set(expected_artifact_sha256) != ids:
        raise ValueError("INVALID_FEATURE_PINS")
    canonical = json.dumps({
        "schema": 2,
        "asset_id": decision.asset_id,
        "decision_at": _utc(decision.decision_at),
        "inputs": sorted(rows, key=lambda row: row["id"]),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    digest = sha256(canonical).hexdigest()
    if expected_receipt_sha256 is not None and digest != expected_receipt_sha256:
        raise ValueError("FEATURE_RECEIPT_MISMATCH")
    return FeatureAvailabilityReceipt(digest, len(rows), news)
