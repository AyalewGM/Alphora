import unittest
from fastapi.testclient import TestClient
from alphora.api.main import app

class TestApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_is_paper_only(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["trading_enabled"])
        self.assertEqual(r.json()["mode"], "paper")

    def test_policy_limits(self):
        r = self.client.get("/v1/risk/policy")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["starting_virtual_equity"], "100000")
        self.assertEqual(r.json()["max_planned_trade_risk_fraction"], "0.005")
        self.assertEqual(r.json()["max_instrument_allocation_fraction"], "0.10")
        self.assertEqual(r.json()["daily_loss_shutdown_fraction"], "0.02")
        self.assertEqual(r.json()["rebalance_frequency"], "daily")

    def test_live_and_paper_order_endpoints_absent(self):
        self.assertEqual(self.client.post("/v1/orders", json={}).status_code, 404)
        self.assertEqual(self.client.post("/v1/live/orders", json={}).status_code, 404)

    def test_no_live_capability(self):
        r = self.client.get("/v1/system/capabilities")
        self.assertFalse(r.json()["live_orders_enabled"])
        self.assertFalse(r.json()["paper_orders_enabled"])
