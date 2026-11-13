"""Validated, inert configuration contract for local *paper-only* research.

A value defined here does not change an existing SQLite book or authorize
dispatch. The paper execution engine must explicitly opt in through a separate,
reviewed migration after the durable session-loss gate is accepted (#42).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re

from .risk import (
    INITIAL_EQUITY, RISK_FRACTION, MAX_INSTRUMENT_FRACTION,
    DAILY_LOSS_FRACTION, REBALANCE_FREQUENCY, RiskPolicy,
)

_ALLOWED_CADENCES = frozenset(("daily", "weekly", "manual"))
_CENT = Decimal("0.01")
_SCHEMA = "paper-risk-proposal-v1"
_DECIMAL_TEXT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_DECIMAL_FIELDS = ("starting_equity_usd", "risk_per_trade_fraction",
                   "max_instrument_fraction", "daily_loss_fraction")
_WIRE_KEYS = frozenset(("schema", "rebalance_frequency", *_DECIMAL_FIELDS))


def _canonical(value: Decimal) -> str:
    # format("f") is exact at all ambient Decimal context precisions.
    literal = format(value, "f")
    if "." in literal:
        literal = literal.rstrip("0").rstrip(".")
    return "0" if literal in ("-0", "") else literal


def _fits_wire_decimal(value: Decimal) -> bool:
    """Bound cost before decimal-to-plain-string expansion.

    Every accepted constructor value must serialize to a strict <=64-character
    decimal accepted by from_wire(). A finite 1e-1000000 is *not* safe to
    expand before validating its tuple exponent.
    """
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        return False
    parts = value.as_tuple()
    if (len(parts.digits) > 64 or parts.exponent < -64
            or parts.exponent > 64):
        return False
    literal = _canonical(value)
    return len(literal) <= 64 and _DECIMAL_TEXT.fullmatch(literal) is not None


def _fraction(value: Decimal, maximum: Decimal) -> bool:
    return _fits_wire_decimal(value) and value <= maximum


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
        # Check the cent quantum exactly even under a low ambient Decimal
        # precision. Reject pathological inputs rather than raising an
        # unexpected decimal exception.
        valid_equity = False
        if _fits_wire_decimal(equity):
            try:
                with localcontext() as ctx:
                    ctx.prec = max(28, len(equity.as_tuple().digits) + 2)
                    valid_equity = equity == equity.quantize(_CENT)
            except (InvalidOperation, OverflowError):
                valid_equity = False
        if (not valid_equity
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

    def to_wire(self) -> dict[str, str]:
        """Strict versioned proposal payload; all amounts are decimal *strings*.

        This is a data-transfer representation, never an active risk policy.
        """
        return {
            "schema": _SCHEMA,
            "starting_equity_usd": _canonical(self.starting_equity_usd),
            "risk_per_trade_fraction": _canonical(self.risk_per_trade_fraction),
            "max_instrument_fraction": _canonical(self.max_instrument_fraction),
            "daily_loss_fraction": _canonical(self.daily_loss_fraction),
            "rebalance_frequency": self.rebalance_frequency,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, str]) -> "PaperRiskConfig":
        """Fail closed on malformed, partial, unknown or imprecise JSON input.

        Float/number coercion is intentionally prohibited: the UI/API must
        send strings, preserving exact Decimal bounds and fingerprints.
        """
        if (type(payload) is not dict or payload.keys() != _WIRE_KEYS
                or payload.get("schema") != _SCHEMA
                or type(payload.get("rebalance_frequency")) is not str):
            raise ValueError("INVALID_PAPER_RISK_CONFIG")
        for name in _DECIMAL_FIELDS:
            value = payload[name]
            if (type(value) is not str or len(value) > 64
                    or not _DECIMAL_TEXT.fullmatch(value)):
                raise ValueError("INVALID_PAPER_RISK_CONFIG")
        return cls(
            starting_equity_usd=Decimal(payload["starting_equity_usd"]),
            risk_per_trade_fraction=Decimal(payload["risk_per_trade_fraction"]),
            max_instrument_fraction=Decimal(payload["max_instrument_fraction"]),
            daily_loss_fraction=Decimal(payload["daily_loss_fraction"]),
            rebalance_frequency=payload["rebalance_frequency"],
        )

    def receipt(self) -> str:
        """Stable proposal receipt, not proof of application or authorization."""
        packed = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True)
        return hashlib.sha256(packed.encode("ascii")).hexdigest()
