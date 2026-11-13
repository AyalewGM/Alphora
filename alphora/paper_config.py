"""Validated, inert configuration contract for local *paper-only* research.

A value defined here does not change an existing SQLite book or authorize
dispatch. The paper execution engine must explicitly opt in through a separate,
reviewed migration after the durable session-loss gate is accepted (#42).
"""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from .risk import (
    INITIAL_EQUITY, RISK_FRACTION, MAX_INSTRUMENT_FRACTION,
    DAILY_LOSS_FRACTION, REBALANCE_FREQUENCY, RiskPolicy,
)

_ALLOWED_CADENCES = frozenset(("daily", "weekly", "manual"))
_CENT = Decimal("0.01")


def _canonical(value: Decimal) -> str:
    # format("f") is exact at all ambient Decimal context precisions.
    literal = format(value, "f")
    if "." in literal:
        literal = literal.rstrip("0").rstrip(".")
    return "0" if literal in ("-0", "") else literal


def _fraction(value: Decimal, maximum: Decimal) -> bool:
    return (type(value) is Decimal and value.is_finite()
            and Decimal("0") < value <= maximum)


@dataclass(frozen=True)
class PaperRiskConfig:
    """Proposed settings, NOT an active or persisted trading authorization.

    Current safety ceilings are deliberately immutable in this initial contract.
    Values below those ceilings are allowed; higher or missing limits fail closed.
    All future effective revisions require atomic persistence/audit under #65.
    """
    starting_equity_usd: Decimal = INITIAL_EQUITY
    risk_per_trade_fraction: Decimal = RISK_FRACTION
    max_instrument_fraction: Decimal = MAX_INSTRUMENT_FRACTION
    daily_loss_fraction: Decimal = DAILY_LOSS_FRACTION
    rebalance_frequency: str = REBALANCE_FREQUENCY

    def __post_init__(self) -> None:
        equity = self.starting_equity_usd
        if (type(equity) is not Decimal or not equity.is_finite()
                or equity <= 0 or equity != equity.quantize(_CENT)
                or not _fraction(self.risk_per_trade_fraction, RISK_FRACTION)
                or not _fraction(self.max_instrument_fraction, MAX_INSTRUMENT_FRACTION)
                or not _fraction(self.daily_loss_fraction, DAILY_LOSS_FRACTION)
                or type(self.rebalance_frequency) is not str
                or self.rebalance_frequency not in _ALLOWED_CADENCES):
            raise ValueError("INVALID_PAPER_RISK_CONFIG")

    def risk_policy(self) -> RiskPolicy:
        """Only the two already supported *tightening* knobs map to the engine."""
        return RiskPolicy(self.risk_per_trade_fraction,
                          self.max_instrument_fraction)

    def receipt(self) -> str:
        """Stable proposal receipt, not proof of application or authorization."""
        payload = {
            "schema": "paper-risk-proposal-v1",
            "starting_equity_usd": _canonical(self.starting_equity_usd),
            "risk_per_trade_fraction": _canonical(self.risk_per_trade_fraction),
            "max_instrument_fraction": _canonical(self.max_instrument_fraction),
            "daily_loss_fraction": _canonical(self.daily_loss_fraction),
            "rebalance_frequency": self.rebalance_frequency,
        }
        packed = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True)
        return hashlib.sha256(packed.encode("ascii")).hexdigest()
