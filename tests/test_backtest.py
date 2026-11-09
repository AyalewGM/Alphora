import unittest
import csv
import tempfile
from dataclasses import replace
from datetime import date,datetime,timedelta
from decimal import Decimal as D, localcontext
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

    def test_csv_rejects_extra_and_missing_columns(self):
        header = "date,open,high,low,close,volume"
        good = "2025-01-01,100,101,99,100,1000"
        other = "2025-01-02,101,102,100,101,1000"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            for bad in (
                good + ",unexpected",  # DictReader returns None: [extra]
                "2025-01-01,100,101,99,100",  # Missing volume
                "2025-01-01,100,101,99,100,",  # Blank volume
            ):
                with self.subTest(row=bad):
                    path.write_text(header + "\n" + bad + "\n" + other + "\n",
                                    encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "BAD_CSV_ROW"):
                        load_csv(path)

    def test_backtest_lookback_requires_strict_integer(self):
        for bad in (True, 2.5, D("20"), "20", None, 0, -5):
            with self.subTest(lookback=repr(bad)):
                with self.assertRaisesRegex(ValueError, "INVALID_PARAMETERS"):
                    backtest(self.bars, "trend", lookback=bad)
                with self.assertRaisesRegex(ValueError, "STRATEGY_OR_LOOKBACK"):
                    signal([b.close for b in self.bars], "trend", lookback=bad)
        self.assertIsInstance(backtest(self.bars, "trend", lookback=2).final_equity, D)

    def test_csv_malformed_quoting_fails_closed(self):
        header = "date,open,high,low,close,volume"
        valid = "2025-01-01,100,101,99,100,1000"
        later = "2025-01-02,101,102,100,101,1000"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            for bad in (
                '2025-01-01,100,101,99,100,"1000',  # Unterminated quote
                '2025-01-01,100,101,99,100,"100"0',  # Garbage after quote
            ):
                with self.subTest(row=bad):
                    path.write_text(header + "\n" + bad + "\n" + later + "\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "BAD_CSV_ROW"):
                        load_csv(path)
            # Well-formed quoted CSV remains supported.
            path.write_text(header + "\n" + valid + "\n" +
                            '2025-01-02,101,102,100,101,"1000"' + "\n",
                            encoding="utf-8")
            self.assertEqual(len(load_csv(path)), 2)

    def test_csv_blank_records_and_empty_file_fail_closed(self):
        header = "date,open,high,low,close,volume"
        first = "2025-01-01,100,101,99,100,1000"
        second = "2025-01-02,101,102,100,101,1000"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CSV_SCHEMA"):
                load_csv(path)

            # DictReader previously skipped these without reporting data loss.
            for rows in (
                "\n" + first + "\n" + second,
                first + "\n\n" + second,
                first + "\n" + second + "\n",
            ):
                with self.subTest(rows=repr(rows)):
                    path.write_text(header + "\n" + rows + "\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "BAD_CSV_ROW"):
                        load_csv(path)

    def test_csv_requires_canonical_dates_and_volumes(self):
        # Python date.fromisoformat and int accept alternative strings that
        # silently change a vendor's source representation.
        header = "date,open,high,low,close,volume"
        second = "2025-01-02,101,102,100,101,1000"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            for day, volume in (
                ("20250101", "1000"),    # Basic date silently normalized
                ("2025-W01-3", "1000"),  # ISO week date silently normalized
                ("2025-01-01", "1_000"), # Python's digit separator
                ("2025-01-01", "+1000"),
                ("2025-01-01", " 1000 "),
                ("2025-01-01", "0001"),
                ("2025-01-01", "-0"),
            ):
                with self.subTest(day=day, volume=volume):
                    row = f"{day},100,101,99,100,{volume}"
                    path.write_text(header + "\n" + row + "\n" + second + "\n",
                                    encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "BAD_CSV_ROW"):
                        load_csv(path)
            # Zero trading volume is valid in the generic daily-bar model;
            # research execution replay applies a separate fillability gate.
            path.write_text(header + "\n" +
                            "2025-01-01,100,101,99,100,0\n" + second + "\n",
                            encoding="utf-8")
            self.assertEqual(load_csv(path)[0].volume, 0)

    def test_csv_requires_canonical_ohlc_prices(self):
        # Decimal accepts these noncanonical strings without reporting an
        # error, obscuring vendor input and reproducibility of research.
        header = "date,open,high,low,close,volume"
        good_later = "2025-01-02,101,102,100,101,1000"
        fields = ("open", "high", "low", "close")
        bad_values = ("1_00", " 100 ", "+100", "1e2", "١٠٠",
                      "00100", "100.", ".5")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            for field in fields:
                for value in bad_values:
                    with self.subTest(field=field, value=value):
                        row = ["2025-01-01", "100", "100", "100", "100", "1000"]
                        row[1 + fields.index(field)] = value
                        path.write_text(header + "\n" + ",".join(row) +
                                        "\n" + good_later + "\n", encoding="utf-8")
                        with self.assertRaisesRegex(ValueError, "BAD_CSV_ROW"):
                            load_csv(path)
            # Preserve canonical integer and fractional decimal prices.
            for price in ("100", "100.00", "0.50", "100.123"):
                with self.subTest(valid_price=price):
                    path.write_text(header + "\n" +
                                    ",".join(["2025-01-01", *([price] * 4), "1000"]) +
                                    "\n" + good_later + "\n", encoding="utf-8")
                    self.assertEqual(load_csv(path)[0].open, D(price))

    def test_inexact_cash_ledger_fails_closed(self):
        # At default Decimal precision, subtracting one dollar from 1e30
        # silently produces 1e30 again. Never count the share without paying.
        tiny = [
            Bar(date(2025, 1, 2), D("1"), D("1"), D("1"), D("1"), 1000),
            Bar(date(2025, 1, 3), D("1"), D("1"), D("1"), D("1"), 1000),
        ]
        with self.assertRaisesRegex(
            ValueError, "UNREPRESENTABLE_BACKTEST_LEDGER_ARITHMETIC"
        ):
            backtest(tiny, "buy_hold", initial=D("1e30"),
                     max_allocation=D("1e-30"), cost_bps=D(0))
        # Normal owner-approved capital and allocation remain usable.
        ordinary = backtest(tiny, "buy_hold", initial=D("100000"),
                            max_allocation=D("0.10"), cost_bps=D(0))
        self.assertEqual(ordinary.equity_curve, (D("100000"), D("100000")))
        self.assertEqual(ordinary.transactions, 1)

    def test_near_integer_share_sizing_ignores_caller_precision(self):
        # With a 6-digit caller Decimal context, ordinary true division
        # previously rounded 10000 / 1.0000001 to 10000 shares, crossing
        # the 10%-of-equity allocation budget; the correct floor is 9999.
        bars = [
            Bar(date(2025, 1, 2), D("1"), D("1"), D("1"), D("1"), 1000),
            Bar(date(2025, 1, 3), D("1.0000001"), D("1.0000002"),
                D("1.0000001"), D("1.0000002"), 1000),
        ]
        for precision in (6, 28, 80):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    result = backtest(bars, "buy_hold", cost_bps=D(0))
                    self.assertEqual(result.transactions, 1)
                    self.assertEqual(result.final_equity, D("100000.0009999"))

    def test_exact_ledger_independent_of_caller_decimal_precision(self):
        # Without a pinned context, this input fails at precision 28 but
        # silently succeeds at 80. The same offline artifact must never have
        # context-dependent cash-ledger acceptance or rejection.
        tiny = [
            Bar(date(2025, 1, 2), D("1"), D("1"), D("1"), D("1"), 1000),
            Bar(date(2025, 1, 3), D("1"), D("1"), D("1"), D("1"), 1000),
        ]
        for precision in (6, 28, 80):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    with self.assertRaisesRegex(
                        ValueError, "UNREPRESENTABLE_BACKTEST_LEDGER_ARITHMETIC"
                    ):
                        backtest(tiny, "buy_hold", initial=D("1e30"),
                                 max_allocation=D("1e-30"), cost_bps=D(0))

    def test_inexact_market_valuation_fails_closed(self):
        # High-precision but finite marks can silently round a held
        # position value. Reject rather than report an invented equity.
        tiny = [
            Bar(date(2025, 1, 2), D("1"), D("1"), D("1"), D("1"), 1000),
            Bar(date(2025, 1, 3), D("1"), D("1.000000000000000000000000001"),
                D("1"), D("1.000000000000000000000000001"), 1000),
        ]
        with self.assertRaisesRegex(
            ValueError, "UNREPRESENTABLE_BACKTEST_LEDGER_ARITHMETIC"
        ):
            backtest(tiny, "buy_hold", cost_bps=D(0))

    def test_signal_decisions_are_independent_of_decimal_precision(self):
        # Previously a precision-6 caller could buy a falling close while a
        # precision-28 caller correctly rejected the same trend signal.
        trend = [D("1.0000000"), D("1.0000011"), D("1.0000010")]
        # The opposite precision-sensitive boundary occurs for mean reversion.
        reversion = [D("1.0000000"), D("1.0000011"), D("1.0000005")]
        for precision in (6, 28, 80):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    self.assertFalse(signal(trend, "trend", lookback=2))
                    self.assertTrue(signal(reversion, "mean_reversion", lookback=2))

    def test_backtest_reports_ignore_caller_decimal_precision(self):
        # Identical exact cash/marks must not give zero daily returns or a
        # missing Sharpe merely because the caller uses 6-digit precision.
        days = (date(2025, 1, 2), date(2025, 1, 3),
                date(2025, 1, 6), date(2025, 1, 7))
        closes = ("1", "1.0000002", "1.0000001", "1.0000003")
        bars = [
            Bar(day, D("1"), D("1.0000005"), D("0.9999995"),
                D(close), 1000000)
            for day, close in zip(days, closes)
        ]
        results = []
        for precision in (6, 28, 80):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    result = backtest(bars, "buy_hold", cost_bps=D(0))
                self.assertEqual(result.final_equity, D("100000.003"))
                self.assertEqual(result.total_return, D("0.00000003"))
                self.assertGreater(result.max_drawdown, D(0))
                self.assertIsNotNone(result.sharpe)
                results.append(result)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_extreme_finite_prices_never_emit_nonfinite_float_reports(self):
        # The Decimal ledger can represent these synthetic marks exactly,
        # but float daily returns or variance cannot. Reject, not inf/NaN
        # Sharpe or a falsely zero Sharpe from overflow.
        first = Bar(date(2025, 1, 2), D("1"), D("1"),
                    D("1"), D("1"), 1000)
        for mark, final_day in (("1e309", False), ("1e200", True)):
            with self.subTest(mark=mark):
                second = Bar(date(2025, 1, 3), D("1"), D(mark),
                             D("1"), D(mark), 1000)
                bars = [first, second]
                if final_day:
                    bars.append(Bar(date(2025, 1, 6), D(mark), D(mark),
                                    D(mark), D(mark), 1000))
                with self.assertRaisesRegex(ValueError, "NONFINITE_RESEARCH_REPORT"):
                    backtest(bars, "buy_hold", max_allocation=D("1"),
                             cost_bps=D(0))

    def test_zero_volume_rejected_before_hypothetical_fills(self):
        # The primitive simulator is also callable without the pinned replay
        # wrapper. Never fabricate an open fill on a no-trade session.
        from alphora.research.benchmarks import compare_with_spy
        for index in (0, 1, len(self.bars) - 1):
            with self.subTest(zero_volume_index=index):
                empty = list(self.bars)
                empty[index] = replace(empty[index], volume=0)
                with self.assertRaisesRegex(
                    ValueError, "ZERO_VOLUME_RESEARCH_BACKTEST_UNSUPPORTED"
                ):
                    backtest(empty, "buy_hold")
                # Benchmark helpers must not claim an investable SPY reference
                # if *either* leg has zero-volume observations.
                with self.assertRaisesRegex(
                    ValueError, "ZERO_VOLUME_RESEARCH_BACKTEST_UNSUPPORTED"
                ):
                    compare_with_spy(self.bars, empty, "buy_hold")
                with self.assertRaisesRegex(
                    ValueError, "ZERO_VOLUME_RESEARCH_BACKTEST_UNSUPPORTED"
                ):
                    compare_with_spy(empty, self.bars, "buy_hold")

    def test_spy_zero_volume_in_lookback_warmup_is_not_sliced_away(self):
        # For a non-buy-hold strategy, comparison starts after warm-up.
        # Every supplied SPY session must still pass the volume gate.
        from alphora.research.benchmarks import compare_with_spy
        for index in (3, 25):
            with self.subTest(spy_zero_index=index):
                spy = list(self.bars)
                spy[index] = replace(spy[index], volume=0)
                with self.assertRaisesRegex(
                    ValueError, "ZERO_VOLUME_RESEARCH_BACKTEST_UNSUPPORTED"
                ):
                    compare_with_spy(self.bars, spy, "trend", lookback=20)
        self.assertIsNotNone(
            compare_with_spy(self.bars, self.bars, "trend", lookback=20)
        )

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

    def test_boolean_or_fractional_volume_rejected(self):
        for invalid_volume in (True, False, 1000.0, D("1000")):
            with self.subTest(volume=repr(invalid_volume)):
                bad=replace(self.bars[0],volume=invalid_volume)
                with self.assertRaisesRegex(ValueError, "INVALID_BARS"):
                    backtest([bad]+self.bars[1:],"buy_hold")

    def test_non_date_or_datetime_bar_day_rejected(self):
        for invalid_day in ("2025-01-01", datetime(2025,1,1), None):
            with self.subTest(day=repr(invalid_day)):
                bad=replace(self.bars[0],day=invalid_day)
                with self.assertRaisesRegex(ValueError, "INVALID_BARS"):
                    backtest([bad]+self.bars[1:],"buy_hold")

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
