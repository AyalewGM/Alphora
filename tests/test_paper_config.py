import unittest
from dataclasses import replace
from decimal import Decimal as D, localcontext

from alphora.paper_config import PaperRiskConfig


class TestPaperRiskConfig(unittest.TestCase):
    def test_existing_defaults_are_preserved(self):
        value = PaperRiskConfig()
        self.assertEqual(value.starting_equity_usd, D("100000"))
        self.assertEqual(value.risk_per_trade_fraction, D("0.005"))
        self.assertEqual(value.max_instrument_fraction, D("0.10"))
        self.assertEqual(value.daily_loss_fraction, D("0.02"))
        self.assertEqual(value.rebalance_frequency, "daily")
        self.assertEqual(value.risk_policy().fingerprint(), "0.005|0.1")

    def test_proposed_values_do_not_loosen_existing_hard_safety_ceilings(self):
        default = PaperRiskConfig()
        for change in (
            {"risk_per_trade_fraction": D("0.00500001")},
            {"max_instrument_fraction": D("0.100001")},
            {"daily_loss_fraction": D("0.020001")},
            {"risk_per_trade_fraction": D("0")},
            {"max_instrument_fraction": D("-0.01")},
            {"daily_loss_fraction": D("NaN")},
            {"starting_equity_usd": D("0")},
            {"starting_equity_usd": D("100.001")},
            {"starting_equity_usd": D("Infinity")},
            {"starting_equity_usd": 100000},
            {"rebalance_frequency": "hourly"},
            {"rebalance_frequency": None},
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_RISK_CONFIG"):
                    replace(default, **change)

    def test_research_proposals_are_hash_stable_under_decimal_context(self):
        default = PaperRiskConfig()
        same = replace(default, starting_equity_usd=D("100000.00"),
                       risk_per_trade_fraction=D("0.00500"))
        tighter = replace(default, risk_per_trade_fraction=D("0.00499999999999999999"))
        for prec in (3, 6, 28, 80):
            with self.subTest(prec=prec), localcontext() as ctx:
                ctx.prec = prec
                self.assertEqual(default.receipt(), same.receipt())
                self.assertNotEqual(default.receipt(), tighter.receipt())

    def test_new_portfolio_proposal_is_not_an_execution_setting(self):
        alternative = PaperRiskConfig(
            starting_equity_usd=D("25000"),
            risk_per_trade_fraction=D("0.0025"),
            max_instrument_fraction=D("0.05"),
            daily_loss_fraction=D("0.01"),
            rebalance_frequency="weekly",
        )
        self.assertEqual(alternative.risk_policy().fingerprint(), "0.0025|0.05")
        self.assertEqual(len(alternative.receipt()), 64)
        self.assertNotEqual(alternative.receipt(), PaperRiskConfig().receipt())


if __name__ == "__main__":
    unittest.main()
