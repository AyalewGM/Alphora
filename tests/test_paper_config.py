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


    def test_wire_roundtrip_preserves_exact_policy_and_receipt(self):
        config = PaperRiskConfig(starting_equity_usd=D("25000.00"),
                                 risk_per_trade_fraction=D("0.00250"),
                                 max_instrument_fraction=D("0.05"),
                                 daily_loss_fraction=D("0.01"),
                                 rebalance_frequency="manual")
        wire = config.to_wire()
        self.assertEqual(wire["schema"], "paper-risk-proposal-v1")
        self.assertEqual(wire["starting_equity_usd"], "25000")
        self.assertEqual(wire["risk_per_trade_fraction"], "0.0025")
        self.assertEqual(set(wire), {"schema", "starting_equity_usd",
                                    "risk_per_trade_fraction",
                                    "max_instrument_fraction",
                                    "daily_loss_fraction",
                                    "rebalance_frequency"})
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as ctx:
                ctx.prec = precision
                decoded = PaperRiskConfig.from_wire(wire)
                self.assertEqual(decoded, config)
                self.assertEqual(decoded.receipt(), config.receipt())

    def test_wire_rejects_partial_unknown_wrong_version_and_float_coercion(self):
        good = PaperRiskConfig().to_wire()
        bad = [
            {key: value for key, value in good.items() if key != "daily_loss_fraction"},
            {**good, "extra": "injected"},
            {**good, "schema": "paper-risk-proposal-v2"},
            {**good, "risk_per_trade_fraction": 0.005},
            {**good, "starting_equity_usd": True},
            {**good, "max_instrument_fraction": None},
            {**good, "rebalance_frequency": "HOURLY"},
            {**good, "daily_loss_fraction": "0.02000000001"},
            {**good, "starting_equity_usd": "100000.001"},
            {**good, "starting_equity_usd": "1e5"},
            {**good, "starting_equity_usd": " 100000"},
            {**good, "starting_equity_usd": "+100000"},
            {**good, "starting_equity_usd": "-100000"},
            {**good, "starting_equity_usd": "000100000"},
            {**good, "starting_equity_usd": "9" * 65},
            {**good, "daily_loss_fraction": "NaN"},
            {**good, "daily_loss_fraction": "Infinity"},
            {**good, "daily_loss_fraction": "0.01\\n"},
            [],
            None,
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_RISK_CONFIG"):
                    PaperRiskConfig.from_wire(payload)


    def test_constructor_rejects_values_outside_versioned_wire_domain(self):
        # #65 independent QA: construction previously accepted 1e-70,
        # while to_wire emitted 72 chars and from_wire rejected >64.
        good = PaperRiskConfig()
        for field in ("risk_per_trade_fraction",
                      "max_instrument_fraction", "daily_loss_fraction"):
            for value in (D("1e-63"), D("1e-70"), D("1e-1000000")):
                with self.subTest(field=field, value=str(value)):
                    with self.assertRaisesRegex(ValueError, "INVALID_PAPER_RISK_CONFIG"):
                        replace(good, **{field: value})
        for value in (D("1e65"), D("1e1000000")):
            with self.subTest(starting_equity_usd=str(value)):
                with self.assertRaisesRegex(ValueError, "INVALID_PAPER_RISK_CONFIG"):
                    replace(good, starting_equity_usd=value)

    def test_tiny_but_representable_risk_is_exactly_roundtrippable(self):
        # Longest permitted '0.<zeros>1' wire string (64 characters).
        tiny = PaperRiskConfig(risk_per_trade_fraction=D("1e-62"))
        payload = tiny.to_wire()
        self.assertEqual(len(payload["risk_per_trade_fraction"]), 64)
        for precision in (3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as ctx:
                ctx.prec = precision
                self.assertEqual(PaperRiskConfig.from_wire(payload), tiny)
                self.assertEqual(PaperRiskConfig.from_wire(payload).receipt(), tiny.receipt())
                # The direct policy used by the offline book must accept
                # every proposal-domain fraction that remains representable.
                self.assertEqual(
                    PaperRiskConfig.from_wire(payload).risk_policy().fingerprint(),
                    tiny.risk_policy().fingerprint(),
                )

if __name__ == "__main__":
    unittest.main()
