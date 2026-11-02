import unittest
import csv
import tempfile
from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal as D
from pathlib import Path
from alphora.research.backtest import Bar,load_csv,signal,backtest

class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.bars=[Bar(date(2025,1,1)+timedelta(days=i),D(100+i),D(101+i),D(99+i),D(100+i),1000) for i in range(30)]

    def test_strict_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bars.csv"
            with path.open("w",newline="") as f:
                w=csv.writer(f);w.writerow(["date","open","high","low","close","volume"])
                for b in self.bars:w.writerow([b.day,b.open,b.high,b.low,b.close,b.volume])
            self.assertEqual(load_csv(path),self.bars)

    def test_duplicate_date_rejected(self):
        with self.assertRaises(ValueError):backtest([self.bars[0],self.bars[0]],"trend")

    def test_invalid_ohlc_rejected(self):
        with self.assertRaises(ValueError):backtest([replace(self.bars[0],high=D(1))]+self.bars[1:],"trend")

    def test_high_below_open_rejected(self):
        bad=replace(self.bars[0], high=D("99.5"))
        with self.assertRaisesRegex(ValueError, "INVALID_BARS"):
            backtest([bad]+self.bars[1:],"buy_hold")

    def test_low_above_close_rejected(self):
        bad=replace(self.bars[0], low=D("100.5"))
        with self.assertRaisesRegex(ValueError, "INVALID_BARS"):
            backtest([bad]+self.bars[1:],"buy_hold")

    def test_valid_extremes_accepted(self):
        self.assertEqual(len(backtest(self.bars,"buy_hold").equity_curve),len(self.bars))

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):backtest([replace(self.bars[0],close=D("NaN"))]+self.bars[1:],"trend")

    def test_signal_warmup(self):
        self.assertFalse(signal([b.close for b in self.bars[:20]],"trend"))
        self.assertTrue(signal([b.close for b in self.bars[:21]],"trend"))

    def test_no_same_close_execution(self):
        r=backtest(self.bars,"buy_hold",cost_bps=D(0))
        self.assertEqual(r.equity_curve[1],D(100000))
        self.assertEqual(r.transactions,1)

    def test_cost_sensitivity(self):
        free=backtest(self.bars,"buy_hold",cost_bps=D(0))
        paid=backtest(self.bars,"buy_hold",cost_bps=D(20))
        self.assertLess(paid.final_equity,free.final_equity)

    def test_allocation_cap(self):
        r=backtest(self.bars,"buy_hold",cost_bps=D(0))
        self.assertGreaterEqual(r.equity_curve[1],D(90000))

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(ValueError):backtest(self.bars,"magic")

    def test_negative_cost_rejected(self):
        with self.assertRaises(ValueError):backtest(self.bars,"trend",cost_bps=D(-1))
