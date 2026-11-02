import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class ResearchBoundaryTests(unittest.TestCase):
    def test_lean_example_is_importable_syntax(self):
        source=(ROOT/"research/lean_spy_baseline/main.py").read_text()
        ast.parse(source)

    def test_research_example_has_no_broker_commands(self):
        source=(ROOT/"research/lean_spy_baseline/main.py").read_text().lower()
        for forbidden in ("tradingclient(", "paper=false", "lean live", "api.alpaca.markets", "submit_order"):
            self.assertNotIn(forbidden, source)

    def test_documented_no_live_execution(self):
        text=(ROOT/"docs/LEAN_EVALUATION.md").read_text()
        self.assertIn("paper-api.alpaca.markets", text)
        self.assertIn("not a production LEAN implementation", text)
