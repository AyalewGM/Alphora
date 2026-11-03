"""Strict daily OHLCV validation and offline next-open backtesting."""
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D
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
        r=csv.DictReader(f)
        if r.fieldnames!=["date","open","high","low","close","volume"]:
            raise ValueError("CSV_SCHEMA")
        bars=[]
        for item in r:
            try:
                if any(v is None or not v.strip() for v in item.values()):
                    raise ValueError("MISSING_FIELD")
                bars.append(Bar(date.fromisoformat(item["date"]),*(D(item[k]) for k in ("open","high","low","close")),int(item["volume"])))
            except (ArithmeticError,ValueError,TypeError) as exc:
                raise ValueError("BAD_CSV_ROW") from exc
    return validate(bars)

def signal(closes,strategy,lookback=20):
    if strategy not in ("trend","momentum","mean_reversion","buy_hold") or lookback<2:
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

def backtest(bars,strategy,*,initial=D("100000"),cost_bps=D("5"),max_allocation=D("0.10"),lookback=20):
    validate(bars)
    if (strategy not in ("trend","momentum","mean_reversion","buy_hold")
        or lookback<2 or not all(isinstance(x,D) and x.is_finite() for x in (initial,cost_bps,max_allocation))
        or initial<=0 or not D(0)<=cost_bps<D(10000)
        or not D(0)<max_allocation<=D(1)):
        raise ValueError("INVALID_PARAMETERS")
    cash=initial
    shares=0
    costs=cost_bps/D(10000)
    equity=[initial]
    transactions=0
    for i in range(1,len(bars)):
        # Only through yesterday's close; fill at today's open.
        want=signal([b.close for b in bars[:i]],strategy,lookback)
        opening=bars[i].open
        if shares and not want:
            cash+=shares*opening*(1-costs)
            shares=0
            transactions+=1
        elif want and not shares:
            quantity=int(min(cash,cash*max_allocation)/(opening*(1+costs)))
            if quantity>0:
                cash-=quantity*opening*(1+costs)
                shares=quantity
                transactions+=1
        equity.append(cash+shares*bars[i].close)
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
