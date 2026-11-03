"""Candidate point-in-time snapshot -> deterministic offline research replay.

No network, broker, order, real-money, scheduler or automated execution paths.
Rechecking a hash is not vendor authentication or calendar/corporate-action QA.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal as D
from hashlib import sha256
import json
from typing import Final, Sequence

from .backtest import Result, backtest
from .benchmarks import BenchmarkComparison, compare_with_spy
from .data_provenance import DataSnapshot, inspect_daily_snapshot

_INITIAL: Final = D("100000")
_MAX_ALLOCATION: Final = D("0.10")
_SUPPORTED_STRATEGIES: Final = frozenset(
    {"trend", "momentum", "mean_reversion", "buy_hold"}
)


@dataclass(frozen=True)
class ResearchReplay:
    symbol: str
    source: str
    source_revision: str
    price_basis: str
    as_of: datetime
    candidate_snapshot_sha256: str
    strategy: str
    result: Result
    session_manifest_matched: bool
    session_manifest_digest_matched: bool
    session_manifest_sha256: str | None
    session_manifest_session_count: int | None
    cost_bps: D
    lookback: int
    input_fingerprint_sha256: str  # Local reproducibility receipt, NOT vendor proof.


@dataclass(frozen=True)
class ResearchComparison:
    strategy_snapshot_sha256: str
    spy_snapshot_sha256: str
    as_of: datetime
    comparison: BenchmarkComparison
    session_manifest_matched: bool
    session_manifest_digest_matched: bool
    session_manifest_sha256: str | None
    session_manifest_session_count: int | None
    cost_bps: D
    lookback: int
    input_fingerprint_sha256: str  # Local reproducibility receipt, NOT vendor proof.


def _checked(snapshot: DataSnapshot, *, expected_sha256: str) -> DataSnapshot:
    if type(snapshot) is not DataSnapshot:
        raise ValueError("INVALID_SNAPSHOT")
    # The caller must pin a digest OUTSIDE the snapshot object. The digest is
    # necessary for reproducibility, but attests neither vendor trust nor
    # session/corporate-action completeness.
    if not isinstance(expected_sha256, str):
        raise ValueError("EXPECTED_DIGEST_REQUIRED")
    observed = inspect_daily_snapshot(
        snapshot.symbol, snapshot.observations,
        asset_type=snapshot.asset_type,
        source=snapshot.source,
        source_revision=snapshot.source_revision,
        price_basis=snapshot.price_basis,
        as_of=snapshot.as_of,
        expected_sha256=expected_sha256,
    )
    if observed.sha256 != snapshot.sha256:
        raise ValueError("SNAPSHOT_WAS_MODIFIED")
    # Do not silently mix adjusted/total-return series into a simulator that
    # has no split/dividend cash-flow engine. RAW remains research-only.
    if observed.price_basis != "RAW_UNADJUSTED":
        raise ValueError("UNSUPPORTED_PRICE_BASIS")
    return observed


def session_manifest_sha256(expected_session_dates: Sequence[date]) -> str:
    """Deterministic receipt for a *caller-supplied* daily-session manifest.

    To be meaningful, the receipt must be pinned independently of the data and
    manifest being replayed; this is no calendar or vendor authentication.
    """
    if (not isinstance(expected_session_dates, (tuple, list))
            or len(expected_session_dates) < 2
            or not all(type(day) is date for day in expected_session_dates)
            or any(b <= a for a, b in zip(
                expected_session_dates, expected_session_dates[1:]))):
        raise ValueError("INVALID_SESSION_MANIFEST")
    canonical = ("alphora-session-manifest-v1\n" +
                 "\n".join(day.isoformat() for day in expected_session_dates))
    return sha256(canonical.encode("ascii")).hexdigest()


def _check_expected_sessions(
    snapshot: DataSnapshot, expected_session_dates: Sequence[date] | None,
    expected_session_manifest_sha256: str | None,
) -> tuple[bool, bool, str | None, int | None]:
    """Check coverage and, when supplied, independently pinned digest equality."""
    if expected_session_dates is None:
        if expected_session_manifest_sha256 is not None:
            raise ValueError("SESSION_MANIFEST_REQUIRED")
        return False, False, None, None
    digest = session_manifest_sha256(expected_session_dates)
    if expected_session_manifest_sha256 is not None:
        if (type(expected_session_manifest_sha256) is not str
                or len(expected_session_manifest_sha256) != 64
                or any(ch not in "0123456789abcdef"
                       for ch in expected_session_manifest_sha256)):
            raise ValueError("INVALID_SESSION_MANIFEST_SHA256")
        if expected_session_manifest_sha256 != digest:
            raise ValueError("SESSION_MANIFEST_HASH_MISMATCH")
    if tuple(r.bar.day for r in snapshot.observations) != tuple(expected_session_dates):
        raise ValueError("SESSION_MANIFEST_MISMATCH")
    return True, expected_session_manifest_sha256 is not None, digest, len(expected_session_dates)


def _input_fingerprint(
    *, kind: str, strategy: str, candidate_sha256: str,
    spy_sha256: str | None, as_of: datetime, cost_bps: D,
    lookback: int, manifest_sha256: str | None, manifest_pinned: bool,
) -> str:
    """Stable digest of *research inputs*, never vendor or result authentication.

    Record a distinct digest if a caller repeats the same trial with or without
    an independently pinned calendar manifest. A value computed from inputs
    supplied by the same actor is not a signature or an attestation.
    """
    canonical = {
        "schema": 1,
        "kind": kind,
        "candidate_snapshot_sha256": candidate_sha256,
        "spy_snapshot_sha256": spy_sha256,
        "as_of_utc": as_of.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "strategy": strategy,
        "cost_bps": format(cost_bps.normalize(), "f"),
        "lookback": lookback,
        "virtual_initial_equity": format(_INITIAL, "f"),
        "max_instrument_allocation": format(_MAX_ALLOCATION, "f"),
        "session_manifest_sha256": manifest_sha256,
        "session_manifest_pinned": manifest_pinned,
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")
    return sha256(data).hexdigest()


def _parameters(strategy: str, lookback: int) -> None:
    if (not isinstance(strategy, str) or strategy not in _SUPPORTED_STRATEGIES
            or type(lookback) is not int or lookback < 2):
        raise ValueError("INVALID_RESEARCH_PARAMETERS")


def replay_candidate_snapshot(
    snapshot: DataSnapshot, strategy: str, *,
    expected_sha256: str, cost_bps: D = D("5"), lookback: int = 20,
    expected_session_dates: Sequence[date] | None = None,
    expected_session_manifest_sha256: str | None = None,
) -> ResearchReplay:
    """Replay an externally pinned, caller-attested candidate dataset.

    The simulator's risk controls are NOT equivalent to execution guards.
    This research-only function fixes virtual initial equity and per-instrument
    entry allocation to owner-approved limits. It cannot place trades.
    """
    _parameters(strategy, lookback)
    checked = _checked(snapshot, expected_sha256=expected_sha256)
    manifest_matched, digest_matched, digest, sessions = _check_expected_sessions(
        checked, expected_session_dates, expected_session_manifest_sha256)
    result = backtest(checked.bars, strategy, initial=_INITIAL,
                      cost_bps=cost_bps, max_allocation=_MAX_ALLOCATION,
                      lookback=lookback)
    fingerprint = _input_fingerprint(
        kind="single_asset", strategy=strategy, candidate_sha256=checked.sha256,
        spy_sha256=None, as_of=checked.as_of, cost_bps=cost_bps,
        lookback=lookback, manifest_sha256=digest, manifest_pinned=digest_matched,
    )
    return ResearchReplay(
        checked.symbol, checked.source, checked.source_revision,
        checked.price_basis, checked.as_of, checked.sha256, strategy, result,
        manifest_matched, digest_matched, digest, sessions, cost_bps, lookback,
        fingerprint,
    )


def compare_candidate_snapshots(
    strategy_snapshot: DataSnapshot, spy_snapshot: DataSnapshot, strategy: str,
    *, expected_strategy_sha256: str, expected_spy_sha256: str,
    cost_bps: D = D("5"), lookback: int = 20,
    expected_session_dates: Sequence[date] | None = None,
    expected_session_manifest_sha256: str | None = None,
) -> ResearchComparison:
    """Compare timestamp-aligned candidate series with price-only SPY proxies.

    The fully-invested SPY reference is a research comparator, explicitly
    NOT a trade-eligible 10%-allocation policy or total-return benchmark.
    """
    _parameters(strategy, lookback)
    candidate = _checked(strategy_snapshot,
                         expected_sha256=expected_strategy_sha256)
    spy = _checked(spy_snapshot, expected_sha256=expected_spy_sha256)
    if spy.symbol != "SPY" or spy.asset_type != "ETF":
        raise ValueError("SPY_BENCHMARK_REQUIRED")
    if candidate.as_of != spy.as_of:
        raise ValueError("BENCHMARK_AS_OF_MISMATCH")
    manifest_matched, digest_matched, digest, sessions = _check_expected_sessions(
        candidate, expected_session_dates, expected_session_manifest_sha256)
    _check_expected_sessions(spy, expected_session_dates,
                             expected_session_manifest_sha256)
    comparison = compare_with_spy(
        candidate.bars, spy.bars, strategy,
        initial=_INITIAL, cost_bps=cost_bps,
        max_allocation=_MAX_ALLOCATION, lookback=lookback
    )
    fingerprint = _input_fingerprint(
        kind="spy_comparison", strategy=strategy,
        candidate_sha256=candidate.sha256, spy_sha256=spy.sha256,
        as_of=candidate.as_of, cost_bps=cost_bps, lookback=lookback,
        manifest_sha256=digest, manifest_pinned=digest_matched,
    )
    return ResearchComparison(
        candidate.sha256, spy.sha256, candidate.as_of, comparison,
        manifest_matched, digest_matched, digest, sessions, cost_bps, lookback,
        fingerprint,
    )
