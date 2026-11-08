"""Pure synthetic SELL receipt replay, never a broker or executable order ledger.

Uses one externally reconciled, fee-inclusive opening BUY inventory. It cannot
verify a fill, timestamp, holdings, cash, session, or a pinned hash's origin.
No persistence, order submission, market data, or connectivity is implemented.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D, DecimalException, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
import re

from .risk import INITIAL_EQUITY

_CENT = D("0.01")
_MAX_MONEY = D("1000000000000000")
_MAX_SHARES = 1_000_000_000
_MAX_RECEIPTS = 10000
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _money(value, *, positive=False):
    if type(value) is not D or not value.is_finite() or not D(0) <= value <= _MAX_MONEY:
        return False
    if positive and value == 0:
        return False
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            return value == value.quantize(_CENT)
    except DecimalException:
        return False


def _shares(value):
    return type(value) is int and 0 < value <= _MAX_SHARES


def _symbol(value):
    return isinstance(value, str) and _SYMBOL.fullmatch(value) is not None


def _session(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


@dataclass(frozen=True)
class PositionCost:
    """Whole shares and total fee-inclusive acquisition basis (not a market mark)."""
    symbol: str
    shares: int
    cost_basis: D


@dataclass(frozen=True)
class SyntheticSellReceipt:
    """Caller-invented fill record; sequence begins at 1 within this batch."""
    receipt_id: str
    sequence: int
    symbol: str
    shares: int
    price: D
    fee: D = D("0")


@dataclass(frozen=True)
class ExitProjection:
    cash: D
    positions: tuple[PositionCost, ...]
    realized_pnl: D
    applied_receipts: int
    receipt_sha256: str  # caller-verifiable fingerprint; NOT source authentication


def project_synthetic_sells(session_id: str, opening_cash: D,
                            opening_positions: tuple[PositionCost, ...],
                            receipts: tuple[SyntheticSellReceipt, ...], *,
                            expected_sha256: str | None = None) -> ExitProjection:
    """Replay hypothetical sells against a balanced initial $100k BUY book.

    Requires all pending BUYs to be resolved externally; cannot change the
    existing SQLite BUY reservation ledger. Rejects oversells, missing receipt
    sequences and conflicting duplicate IDs. Never allows another session's
    hypothetical receipts to be silently joined into this projection.
    """
    if (not _session(session_id) or not _money(opening_cash)
            or not isinstance(opening_positions, tuple)
            or not isinstance(receipts, tuple) or len(receipts) > _MAX_RECEIPTS
            or (expected_sha256 is not None and
                (not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256)))):
        raise ValueError("INVALID_EXIT_REPLAY_INPUT")
    positions = {}
    for item in opening_positions:
        if (not isinstance(item, PositionCost) or not _symbol(item.symbol)
                or not _shares(item.shares) or not _money(item.cost_basis, positive=True)
                or item.symbol in positions):
            raise ValueError("INVALID_OPENING_INVENTORY")
        positions[item.symbol] = (item.shares, item.cost_basis)
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            if opening_cash + sum((p.cost_basis for p in opening_positions), D(0)) != INITIAL_EQUITY:
                raise ValueError("UNBALANCED_OPENING_BOOK")
            cash = opening_cash
            realized = D(0)
            seen = {}
            canonical_receipts = []
            for receipt in receipts:
                if (not isinstance(receipt, SyntheticSellReceipt)
                        or not isinstance(receipt.receipt_id, str)
                        or not _ID.fullmatch(receipt.receipt_id)
                        or type(receipt.sequence) is not int or receipt.sequence <= 0
                        or not _symbol(receipt.symbol) or not _shares(receipt.shares)
                        or not _money(receipt.price, positive=True)
                        or not _money(receipt.fee)):
                    raise ValueError("INVALID_SYNTHETIC_SELL_RECEIPT")
                prior = seen.get(receipt.receipt_id)
                if prior is not None:
                    if prior != receipt:
                        raise ValueError("CONFLICTING_SELL_RECEIPT_ID")
                    continue  # exact retry of an already accounted receipt
                if receipt.sequence != len(seen) + 1:
                    raise ValueError("SELL_RECEIPT_SEQUENCE_GAP")
                held, basis = positions.get(receipt.symbol, (0, D(0)))
                if receipt.shares > held:
                    raise ValueError("SYNTHETIC_OVERSELL")
                gross = receipt.price * receipt.shares
                if not _money(gross) or receipt.fee > gross:
                    raise ValueError("INVALID_SYNTHETIC_SELL_VALUE")
                proceeds = gross - receipt.fee
                if receipt.shares == held:
                    released_basis = basis  # last share absorbs prior rounding
                    del positions[receipt.symbol]
                else:
                    released_basis = (basis * receipt.shares / held).quantize(
                        _CENT, rounding=ROUND_HALF_EVEN)
                    positions[receipt.symbol] = (held - receipt.shares, basis - released_basis)
                cash += proceeds
                realized += proceeds - released_basis
                if not _money(cash) or not _money(realized.copy_abs()):
                    raise ValueError("SYNTHETIC_SELL_ACCOUNTING_OVERFLOW")
                seen[receipt.receipt_id] = receipt
                canonical_receipts.append({
                    "id": receipt.receipt_id, "seq": receipt.sequence,
                    "symbol": receipt.symbol, "shares": receipt.shares,
                    "price": f"{receipt.price:.2f}", "fee": f"{receipt.fee:.2f}",
                })
            document = {
                "schema": 1, "session_id": session_id, "opening_cash": f"{opening_cash:.2f}",
                "opening_positions": [{"symbol": p.symbol, "shares": p.shares,
                                       "cost_basis": f"{p.cost_basis:.2f}"}
                                      for p in sorted(opening_positions, key=lambda p: p.symbol)],
                "receipts": canonical_receipts,
            }
            digest = sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode("ascii")).hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise ValueError("SELL_RECEIPT_HASH_MISMATCH")
            return ExitProjection(cash, tuple(PositionCost(sym, *positions[sym])
                                              for sym in sorted(positions)),
                                  realized, len(seen), digest)
    except DecimalException as exc:
        raise ValueError("INVALID_SYNTHETIC_SELL_ARITHMETIC") from exc
