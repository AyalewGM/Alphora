"""Local-only, read-only FastAPI shell. No order submission endpoints."""
from fastapi import FastAPI
from alphora.risk import INITIAL_EQUITY, RISK_FRACTION, MAX_INSTRUMENT_FRACTION, DAILY_LOSS_FRACTION, REBALANCE_FREQUENCY

app = FastAPI(
    title="Alphora",
    version="0.1.0",
    description="Private quantitative research API. Paper-only; no broker execution.",
    docs_url="/docs",
)

@app.get("/health")
def health():
    return {"status": "ok", "trading_enabled": False, "mode": "paper"}

@app.get("/v1/system/capabilities")
def capabilities():
    return {
        "market": ["US_STOCKS", "US_ETFS"],
        "research_enabled": True,
        "paper_orders_enabled": False,
        "live_orders_enabled": False,
        "rebalance_frequency": REBALANCE_FREQUENCY,
    }

@app.get("/v1/risk/policy")
def risk_policy():
    return {
        "mode": "paper",
        "starting_virtual_equity": str(INITIAL_EQUITY),
        "max_planned_trade_risk_fraction": str(RISK_FRACTION),
        "max_instrument_allocation_fraction": str(MAX_INSTRUMENT_FRACTION),
        "daily_loss_shutdown_fraction": str(DAILY_LOSS_FRACTION),
        "rebalance_frequency": REBALANCE_FREQUENCY,
    }
