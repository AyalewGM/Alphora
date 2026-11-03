"""Static, offline fail-safe tripwire against accidental broker execution code.

This is a defense-in-depth source check, not a substitute for code review or
runtime network isolation. Brokerage connectivity requires separate authorization.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", ".venv", "venv", "site-packages", "__pycache__"}
BROKER_DISTRIBUTIONS = ("alpaca-py", "alpaca-trade-api", "ib-insync", "ibapi", "ccxt", "robin-stocks", "python-binance")
BROKER_ENV_NAMES = {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_API_BASE_URL"}
BROKER_IMPORT_ROOTS = frozenset({
    "alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt", "robin_stocks",
    "binance",
})
ORDER_SUBMISSION_METHODS = frozenset({
    "submit_order", "submit_orders", "place_order", "place_orders",
    "send_order", "send_orders", "liquidate_position",
    "close_position", "close_all_positions", "create_order",
    "create_market_buy_order", "create_limit_buy_order", "placeOrder",
    "replace_order", "replace_order_by_id", "cancel_order",
})
BROKER_HOST_MARKERS = ("alpaca.markets",)
NETWORK_SINKS = {"get", "post", "put", "patch", "delete", "request", "urlopen", "connect", "create_connection"}


def violations(source: str, *, ignore_fixture_literals: bool = False) -> list[str]:
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
            if (name in ORDER_SUBMISSION_METHODS or
                    (isinstance(name, str) and
                     name.startswith(("order_buy_", "order_sell_")))):
                problems.append(f"line {node.lineno}: order execution call {name}")
            if name == "TradingClient" and any(
                kw.arg == "paper" and isinstance(kw.value, ast.Constant)
                and kw.value.value is False for kw in node.keywords
            ):
                problems.append(f"line {node.lineno}: live broker client")
            # These dynamic usages remain executable even in a file containing
            # intentionally prohibited strings as static regression fixtures.
            if name in ("__import__", "import_module") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    module = first.value.lower().strip()
                    if (module in BROKER_IMPORT_ROOTS or any(
                            module.startswith(root + ".") for root in BROKER_IMPORT_ROOTS)):
                        problems.append(f"line {node.lineno}: dynamic broker import")
            if name in ("getattr", "__getattribute__") and len(node.args) >= 2:
                method = node.args[1]
                if isinstance(method, ast.Constant) and isinstance(method.value, str):
                    value = method.value
                    if (value in ORDER_SUBMISSION_METHODS or
                            value.startswith(("order_buy_", "order_sell_"))):
                        problems.append(f"line {node.lineno}: dynamic broker method")
            if name in NETWORK_SINKS:
                args = list(node.args) + [kw.value for kw in node.keywords]
                for argument in args:
                    if (isinstance(argument, ast.Constant) and
                            isinstance(argument.value, str) and
                            any(host in argument.value.lower()
                                for host in BROKER_HOST_MARKERS)):
                        problems.append(f"line {node.lineno}: broker network request")
                        break
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ignore_fixture_literals:
                continue
            value = node.value.lower().strip()
            # Includes attempts to import SDKs dynamically using literal strings.
            if value in BROKER_IMPORT_ROOTS or any(
                value.startswith(root + ".") for root in BROKER_IMPORT_ROOTS
            ):
                problems.append(f"line {node.lineno}: broker module literal")
            elif any(host in value for host in BROKER_HOST_MARKERS):
                problems.append(f"line {node.lineno}: broker endpoint literal")
            elif value.upper() in BROKER_ENV_NAMES:
                problems.append(f"line {node.lineno}: broker credential name")
            elif (value in ORDER_SUBMISSION_METHODS or
                  value.startswith(("order_buy_", "order_sell_"))):
                # Catches getattr(client, 'submit_order') without flagging prose.
                problems.append(f"line {node.lineno}: order execution literal {value}")
    return problems


class BrokerBoundaryTests(unittest.TestCase):
    def test_repository_has_no_broker_imports_or_execution_calls(self):
        problems = []
        for path in sorted(ROOT.rglob("*.py")):
            if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
                continue
            fixture_path = path.resolve() in {
                Path(__file__).resolve(),
                (ROOT / "tests" / "test_research_boundary.py").resolve(),
            }
            try:
                # Check *all* Python modules for imports and executable broker
                # calls, including fixture modules. Only their bare test strings
                # are exempt from generic literal scanning.
                problems.extend(
                    f"{path.relative_to(ROOT)}: {msg}"
                    for msg in violations(path.read_text(encoding="utf-8"),
                                          ignore_fixture_literals=fixture_path)
                )
            except SyntaxError as exc:
                problems.append(f"{path.relative_to(ROOT)}: invalid Python: {exc}")
        # Also prevent accidental broker dependency introduction.
        dependency_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
        for distribution in BROKER_DISTRIBUTIONS:
            if distribution in dependency_text:
                problems.append(f"pyproject.toml: broker dependency {distribution}")
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
            "ex.create_order('AAPL', 'market', 'buy', 1)",
            "ex.create_market_buy_order('AAPL', 1)",
            "ex.create_limit_buy_order('AAPL', 1, 100)",
            "ib.placeOrder(contract, order)",
            "rh.order_buy_market('AAPL', 1)",
            "rh.order_sell_market('AAPL', 1)",
            "client.replace_order_by_id('abc')",
            "client.replace_order(order)",
            "client.cancel_order(order)",
            "url = 'https://broker-api.sandbox.alpaca.markets/v1'",
            "url = 'wss://stream.data.alpaca.markets'",
            "TradingClient('dummy', 'dummy', paper=False)",
            "keyname = 'APCA_API_KEY_ID'",
            "keyname = 'APCA_API_SECRET_KEY'",
            "keyname = 'APCA_API_BASE_URL'",
        )
        for source in unsafe:
            with self.subTest(source=source):
                self.assertTrue(violations(source))

    def test_fixture_module_exemption_does_not_hide_executable_broker_usage(self):
        for source in (
            "from alpaca.trading.client import TradingClient",
            "client.submit_order(order)",
            "client.create_order(symbol='AAPL')",
            "m = __import__('alpaca.trading.client')",
            "m = importlib.import_module('alpaca.trading.client')",
            "method = getattr(broker, 'submit_order')",
            "requests.post('https://paper-api.alpaca.markets/v2/orders')",
        ):
            with self.subTest(source=source):
                self.assertTrue(violations(source, ignore_fixture_literals=True))
        self.assertEqual(
            violations("text = 'paper-api.alpaca.markets'",
                       ignore_fixture_literals=True), [])

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
