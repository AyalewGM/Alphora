"""LEAN offline backtesting example. Never invokes a brokerage API."""
from AlgorithmImports import *

class AlphoraSpyBaseline(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2020, 1, 2)
        self.set_end_date(2023, 12, 29)
        self.set_cash(100000)
        self.set_benchmark("SPY")
        self.settings.free_portfolio_value_percentage = 0.01
        security = self.add_equity("SPY", Resolution.DAILY)
        security.set_data_normalization_mode(DataNormalizationMode.ADJUSTED)
        self.symbol = security.symbol
        self.set_warm_up(30, Resolution.DAILY)

    def on_data(self, data: Slice):
        if self.is_warming_up or not data.contains_key(self.symbol):
            return
        # 10% allocation is a constrained research example, NOT a full SPY benchmark.
        if not self.portfolio[self.symbol].invested:
            self.set_holdings(self.symbol, 0.10)
