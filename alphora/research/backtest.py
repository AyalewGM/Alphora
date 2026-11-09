"""Strict daily OHLCV validation and offline next-open backtesting."""
import csv
import re
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal as D, DecimalException, Inexact, localcontext
from math import sqrt

@dataclass(frozen=True)
class Bar:
    day: date
    open: D
    high: D
    low: D
    close: D
    volume: int

def validate(bars):
    if len(bars) < 2: raise ValueError("INSUFFICIENT_BARS")
    for i,b in enumerate(bars):
        p=(b.open,b.high,b.low,b.close)
        if (not all(isinstance(x,D) and x.is_finite() and x>0 for x in p)
            or type(b.day) is not date
            or type(b.volume) is not int or b.volume<0
            or b.low>min(b.open,b.close,b.high) or b.high<max(b.open,b.close,b.low)
            or (i and b.day<=bars[i-1].day)):
            raise ValueError("INVALID_BARS")
    return bars

def load_csv(path):
    with open(path,encoding="utf-8",newline="") as f:
        # csv.DictReader silently skips blank records, which can hide
        # missing sessions in historical replay. Inspect every CSV record.
        r=csv.reader(f,strict=True)
        try:
            header=next(r)
        except (csv.Error, StopIteration) as exc:
            raise ValueError("CSV_SCHEMA") from exc
        if header!=["date","open","high","low","close","volume"]:
            raise ValueError("CSV_SCHEMA")
        bars=[]
        while True:
            try:
                row=next(r)
            except StopIteration:
                break
            except csv.Error as exc:
                # Malformed quoting may otherwise silently swallow future rows.
                raise ValueError("BAD_CSV_ROW") from exc
            try:
                # Reject blank records, truncated rows, and surplus columns
                # without silently dropping a potential historical session.
                if len(row)!=len(header) or any(not v.strip() for v in row):
                    raise ValueError("MISSING_OR_EXTRA_FIELD")
                item=dict(zip(header,row))
                # date.fromisoformat accepts compact/week dates; int accepts
                # underscores, signs and whitespace. Reject such silently
                # normalized vendor values rather than changing their meaning.
                if (not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", item["date"])
                    or not re.fullmatch(r"(?:0|[1-9][0-9]*)", item["volume"])
                    # Decimal accepts underscores, Unicode digits, signs, and
                    # exponent notation; reject implicit vendor normalization.
                    or any(not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", item[k])
                           for k in ("open", "high", "low", "close"))):
                    raise ValueError("NONCANONICAL_CSV_VALUE")
                bars.append(Bar(date.fromisoformat(item["date"]),*(D(item[k]) for k in ("open","high","low","close")),int(item["volume"])))
            except (ArithmeticError,ValueError,TypeError) as exc:
                raise ValueError("BAD_CSV_ROW") from exc
    return validate(bars)

def signal(closes,strategy,lookback=20):
    if (strategy not in ("trend","momentum","mean_reversion","buy_hold")
            or type(lookback) is not int or lookback < 2):
        raise ValueError("STRATEGY_OR_LOOKBACK")
    if strategy=="buy_hold": return True
    if len(closes)<lookback+1: return False
    if strategy=="momentum": return closes[-1]>closes[-lookback-1]
    if strategy=="trend": return closes[-1]>sum(closes[-lookback:],D(0))/lookback
    return closes[-1]<sum(closes[-lookback-1:-1],D(0))/lookback

@dataclass(frozen=True)
class Result:
    strategy: str
    final_equity: D
    total_return: D
    max_drawdown: D
    sharpe: float | None
    transactions: int
    equity_curve: tuple[D,...]

# Fix ledger precision independent of caller-global Decimal context. The
# default 28 significant digits remain the conservative accounting boundary.
_LEDGER_CONTEXT = Context(prec=28)
_LEDGER_CONTEXT.traps[Inexact] = True


def _exact_ledger(operation):
    """Reject rounded/overflowing simulated cash and market-value arithmetic.

    Fractional sizing and performance ratios may be rounded for research, but
    the cash ledger must not silently lose dollars or small positions.
    """
    try:
        with localcontext(_LEDGER_CONTEXT):
            return operation()
    except DecimalException as exc:
        raise ValueError("UNREPRESENTABLE_BACKTEST_LEDGER_ARITHMETIC") from exc


def backtest(bars,strategy,*,initial=D("100000"),cost_bps=D("5"),max_allocation=D("0.10"),lookback=20):
    validate(bars)
    # A positive open price on a zero-volume session does not establish a
    # tradable next-open fill. Until exchange halts and stale prints are
    # modeled, fail closed rather than invent trades or optimistic P&L.
    if any(b.volume == 0 for b in bars):
        raise ValueError("ZERO_VOLUME_RESEARCH_BACKTEST_UNSUPPORTED")
    if (strategy not in ("trend","momentum","mean_reversion","buy_hold")
        or type(lookback) is not int or lookback < 2
        or not all(isinstance(x,D) and x.is_finite() for x in (initial,cost_bps,max_allocation))
        or initial<=0 or not D(0)<=cost_bps<D(10000)
        or not D(0)<max_allocation<=D(1)):
        raise ValueError("INVALID_PARAMETERS")
    cash=initial
    shares=0
    costs=_exact_ledger(lambda: cost_bps/D(10000))
    equity=[initial]
    transactions=0
    for i in range(1,len(bars)):
        # Only through yesterday's close; fill at today's open.
        want=signal([b.close for b in bars[:i]],strategy,lookback)
        opening=bars[i].open
        if shares and not want:
            cash=_exact_ledger(lambda: cash+shares*opening*(1-costs))
            shares=0
            transactions+=1
        elif want and not shares:
            budget=_exact_ledger(lambda: min(cash,cash*max_allocation))
            unit_cost=_exact_ledger(lambda: opening*(1+costs))
            # Decimal true division may round an almost-integer share count
            # upward under a caller's low precision (violating the cap).
            # Integer division is exact and pinned to the ledger context.
            quantity=int(_exact_ledger(lambda: budget // unit_cost))
            if quantity>0:
                cash=_exact_ledger(lambda: cash-quantity*unit_cost)
                shares=quantity
                transactions+=1
        equity.append(_exact_ledger(lambda: cash+shares*bars[i].close))
    peak=equity[0]
    drawdown=D(0)
    returns=[]
    for a,b in zip(equity,equity[1:]):
        peak=max(peak,b)
        drawdown=max(drawdown,(peak-b)/peak)
        returns.append(float(b/a-1))
    sharpe=None
    if len(returns)>1:
        mean=sum(returns)/len(returns)
        variance=sum((x-mean)**2 for x in returns)/(len(returns)-1)
        if variance>0: sharpe=sqrt(252)*mean/sqrt(variance)
    return Result(strategy,equity[-1],equity[-1]/initial-1,drawdown,sharpe,transactions,tuple(equity))
