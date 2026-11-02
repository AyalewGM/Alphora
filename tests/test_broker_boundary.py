"""Static, offline fail-safe tripwire against accidental broker execution code.

This is a defense-in-depth source check, not a substitute for code review or
runtime network isolation. Brokerage connectivity requires separate authorization.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = (ROOT / "alphora", ROOT / "research")
BROKER_IMPORT_ROOTS = frozenset({
    "alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt", "robin_stocks",
    "binance",
})
ORDER_SUBMISSION_METHODS = frozenset({
    "submit_order", "submit_orders", "place_order", "place_orders",
    "send_order", "send_orders", "liquidate_position",
    "close_position", "close_all_positions",
})
BROKER_HOST_MARKERS = ("api.alpaca.markets", "paper-api.alpaca.markets")


def violations(source: str) -> list[str]:
    """Find known unsafe constructs in Python code; all matches are fail-closed."""
    tree = ast.parse(source)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BROKER_IMPORT_ROOTS:
                    problems.append(f"line {node.lineno}: broker import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BROKER_IMPORT_ROOTS:
                problems.append(f"line {node.lineno}: broker import {node.module}")
        elif isinstance(node, ast.Call):
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else (
                target.id if isinstance(target, ast.Name) else None
            )
            if name in ORDER_SUBMISSION_METHODS:
                problems.append(f"line {node.lineno}: order execution call {name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.lower().strip()
            # Includes attempts to import SDKs dynamically using literal strings.
            if value in BROKER_IMPORT_ROOTS or any(
                value.startswith(root + ".") for root in BROKER_IMPORT_ROOTS
            ):
                problems.append(f"line {node.lineno}: broker module literal")
            elif any(host in value for host in BROKER_HOST_MARKERS):
                problems.append(f"line {node.lineno}: broker endpoint literal")
            elif value in ORDER_SUBMISSION_METHODS:
                # Catches getattr(client, 'submit_order') without flagging prose.
                problems.append(f"line {node.lineno}: order execution literal {value}")
    return problems


class BrokerBoundaryTests(unittest.TestCase):
    def test_repository_has_no_broker_imports_or_execution_calls(self):
        problems = []
        for directory in SCANNED:
            self.assertTrue(directory.is_dir(), f"missing source directory: {directory}")
            for path in sorted(directory.rglob("*.py")):
                try:
                    problems.extend(f"{path.relative_to(ROOT)}: {msg}"
                                    for msg in violations(path.read_text(encoding="utf-8")))
                except SyntaxError as exc:
                    problems.append(f"{path.relative_to(ROOT)}: invalid Python: {exc}")
        self.assertEqual(problems, [], "No broker connectivity is authorized")

    def test_known_broker_paths_are_rejected(self):
        unsafe = (
            "from alpaca.trading.client import TradingClient",
            "import alpaca_trade_api as sdk",
            "import ccxt",
            "broker.submit_order(order)",
            "place_order(order)",
            "client.close_all_positions()",
            "method = getattr(broker, 'submit_order')",
            "module = __import__('alpaca.trading.client')",
            "url = 'https://api.alpaca.markets/v2/orders'",
            "url = 'https://paper-api.alpaca.markets/v2/orders'",
        )
        for source in unsafe:
            with self.subTest(source=source):
                self.assertTrue(violations(source))

    def test_offline_paper_proposals_and_explanatory_prose_allowed(self):
        safe = '''"""This module never submits broker orders or calls submit_order."""
from dataclasses import dataclass
@dataclass(frozen=True)
class PaperIntent:
    symbol: str
    quantity: int
    mode: str = "paper"
def propose(symbol):
    return PaperIntent(symbol, 1)
'''
        self.assertEqual(violations(safe), [])


if __name__ == "__main__":
    unittest.main()
