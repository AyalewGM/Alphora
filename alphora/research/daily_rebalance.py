"""Synthetic price-only daily-open target allocation reference (#20).

Not an executable strategy, total-return series, or continuous risk control.
The opening mark and assumed fill coincide; this is an idealized benchmark,
not evidence that an order sized from the opening print could fill at it.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal as D, localcontext

from .backtest import _exact_ledger, validate
from .benchmarks import AlignedWindowMetrics, _aligned_metrics


@dataclass(frozen=True)
class RebalanceObservation:
    day: date
    shares: int
    cash: D
    trade_shares: int
    fee: D
    traded_notional: D
    pre_open_equity: D
    post_open_equity: D
    open_exposure: D
    close_equity: D
    close_exposure: D
    pre_open_above_target: bool
    close_above_target: bool


@dataclass(frozen=True)
class DailyRebalancedReference:
    observations: tuple[RebalanceObservation, ...]
    metrics: AlignedWindowMetrics
    total_fees: D
    total_traded_notional: D


def _step(bar, cash, shares, target, fee_rate):
    """Run inside the exact, pinned ledger context."""
    price = bar.open
    holding = shares * price
    equity = cash + holding
    excess = holding - target * equity
    # Solve for post-fee marked weight, not the pre-fee equity target.
    # Buy: q*p*(1+a*f) <= a*E-H.
    # Sell: q*p*(1-a*f) >= H-a*E.
    if excess > 0:
        whole, remainder = divmod(excess, price * (1 - target * fee_rate))
        delta = -min(shares, int(whole) + int(remainder > 0))
    else:
        delta = int(min((-excess) // (price * (1 + target * fee_rate)),
                        cash // (price * (1 + fee_rate))))
    notional = abs(delta) * price
    fee = notional * fee_rate
    cash = cash - delta * price - fee
    shares += delta
    marked = shares * price
    post = cash + marked
    closing_mark = shares * bar.close
    closing = cash + closing_mark
    if cash < 0 or shares < 0 or post <= 0 or post != equity - fee or marked > target * post:
        raise ValueError("INVALID_REBALANCE_ACCOUNTING")
    return (cash, shares, delta, fee, notional, equity, post, marked, closing,
            closing_mark, excess > 0, closing_mark > target * closing)


def daily_open_reference(bars, *, initial=D("100000"),
                         target=D("0.10"), cost_bps=D("5")):
    """Whole-share daily open rebalance; first bar is initial cash observation.

    Caller must supply an already aligned, ordered synthetic price window.
    No dividends, splits, interest, spread, slippage, liquidity or calendar
    authentication; fixed proportional transaction fee and no terminal sale.
    """
    validate(bars)
    if any(b.volume == 0 for b in bars):
        raise ValueError("ZERO_VOLUME_RESEARCH_BACKTEST_UNSUPPORTED")
    if (not all(isinstance(x, D) and x.is_finite()
                for x in (initial, target, cost_bps))
            or initial <= 0 or not D(0) < target <= D("0.10")
            or not D(0) <= cost_bps < D("10000")):
        raise ValueError("INVALID_REBALANCE_PARAMETERS")
    fee_rate = _exact_ledger(lambda: cost_bps / D(10000))
    cash, shares = initial, 0
    observations = [RebalanceObservation(bars[0].day, 0, cash, 0, D(0), D(0),
                                        cash, cash, D(0), cash, D(0), False, False)]
    for bar in bars[1:]:
        (cash, shares, delta, fee, notional, before, after, marked, close,
         close_mark, pre_breach, close_breach) = _exact_ledger(
            lambda: _step(bar, cash, shares, target, fee_rate))
        with localcontext(Context(prec=28)):
            open_weight = marked / after
            close_weight = close_mark / close
        observations.append(RebalanceObservation(
            bar.day, shares, cash, delta, fee, notional, before, after,
            open_weight, close, close_weight, pre_breach, close_breach))
    return DailyRebalancedReference(
        tuple(observations),
        _aligned_metrics(tuple(row.close_equity for row in observations)),
        _exact_ledger(lambda: sum((row.fee for row in observations), D(0))),
        _exact_ledger(lambda: sum((row.traded_notional for row in observations), D(0))),
    )
