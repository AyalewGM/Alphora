"""Candidate point-in-time snapshot -> deterministic offline research replay.

No network, broker, order, real-money, scheduler or automated execution paths.
Rechecking a hash is not vendor authentication or calendar/corporate-action QA.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal as D
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
    cost_bps: D
    lookback: int


@dataclass(frozen=True)
class ResearchComparison:
    strategy_snapshot_sha256: str
    spy_snapshot_sha256: str
    as_of: datetime
    comparison: BenchmarkComparison
    session_manifest_matched: bool
    cost_bps: D
    lookback: int


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


def _check_expected_sessions(
    snapshot: DataSnapshot, expected_session_dates: Sequence[date] | None
) -> bool:
    """Compare actual bar dates with a separately supplied session manifest.

    Equality is an auditable coverage condition, not an exchange-calendar or
    vendor authenticity certification. A missing manifest is explicitly
    indicated in the replay result rather than silently called verified.
    """
    if expected_session_dates is None:
        return False
    if (not isinstance(expected_session_dates, (tuple, list))
            or len(expected_session_dates) < 2
            or not all(type(day) is date for day in expected_session_dates)
            or any(b <= a for a, b in zip(
                expected_session_dates, expected_session_dates[1:]))):
        raise ValueError("INVALID_SESSION_MANIFEST")
    if tuple(r.bar.day for r in snapshot.observations) != tuple(expected_session_dates):
        raise ValueError("SESSION_MANIFEST_MISMATCH")
    return True


def _parameters(strategy: str, lookback: int) -> None:
    if (not isinstance(strategy, str) or strategy not in _SUPPORTED_STRATEGIES
            or type(lookback) is not int or lookback < 2):
        raise ValueError("INVALID_RESEARCH_PARAMETERS")


def replay_candidate_snapshot(
    snapshot: DataSnapshot, strategy: str, *,
    expected_sha256: str, cost_bps: D = D("5"), lookback: int = 20,
    expected_session_dates: Sequence[date] | None = None,
) -> ResearchReplay:
    """Replay an externally pinned, caller-attested candidate dataset.

    The simulator's risk controls are NOT equivalent to execution guards.
    This research-only function fixes virtual initial equity and per-instrument
    entry allocation to owner-approved limits. It cannot place trades.
    """
    _parameters(strategy, lookback)
    checked = _checked(snapshot, expected_sha256=expected_sha256)
    manifest_matched = _check_expected_sessions(checked, expected_session_dates)
    result = backtest(checked.bars, strategy, initial=_INITIAL,
                      cost_bps=cost_bps, max_allocation=_MAX_ALLOCATION,
                      lookback=lookback)
    return ResearchReplay(
        checked.symbol, checked.source, checked.source_revision,
        checked.price_basis, checked.as_of, checked.sha256, strategy, result,
        manifest_matched, cost_bps, lookback,
    )


def compare_candidate_snapshots(
    strategy_snapshot: DataSnapshot, spy_snapshot: DataSnapshot, strategy: str,
    *, expected_strategy_sha256: str, expected_spy_sha256: str,
    cost_bps: D = D("5"), lookback: int = 20,
    expected_session_dates: Sequence[date] | None = None,
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
    manifest_matched = _check_expected_sessions(candidate, expected_session_dates)
    _check_expected_sessions(spy, expected_session_dates)
    comparison = compare_with_spy(
        candidate.bars, spy.bars, strategy,
        initial=_INITIAL, cost_bps=cost_bps,
        max_allocation=_MAX_ALLOCATION, lookback=lookback
    )
    return ResearchComparison(
        candidate.sha256, spy.sha256, candidate.as_of, comparison,
        manifest_matched, cost_bps, lookback,
    )
