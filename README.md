# Alphora

**Local-first personal quantitative research platform for US stocks and ETFs.**

> PAPER ONLY. No real-money brokerage execution is implemented or authorized.

## Initial risk policy
- Virtual starting equity: **$100,000**.
- Planned risk per trade: **0.5% of current equity** ($500 initially).
- Maximum allocation per instrument: **10% of current equity** ($10,000 initially).
- Daily new-buy halt at **2% of session opening equity** ($2,000 initially).
- Rebalancing cadence: **daily**.
- Cash-only, long-only. No leverage or short selling.

Risk is based on intended stop distance; slippage and gaps can cause losses beyond a planned stop. The risk guard does not place orders, close positions or claim to enforce the limit without an execution adapter and persistent state.

## First implementation
`alphora/risk.py` provides a pure paper-only pre-trade validation function. `tests/test_risk.py` covers boundary and rejection cases. Run:

```bash
python -m unittest discover -s tests -v
```

## Migration audit
The attached 2023 GitLab `trader` archive contained research code based on AI4Finance FinRL 0.3.6, RL experiments using A2C, PPO and DDPG, a Flask UI, and Alpaca integrations. The old Flask/Alpaca code is **unsafe to reuse unmodified** because form inputs select paper versus live trading and some paths use `paper=False`. It was excluded from runtime migration. See [migration assessment](docs/MIGRATION.md).

Original FinRL source is MIT-licensed © 2020 AI4Finance Foundation. Future optional FinRL integration must retain appropriate copyright notices and use separately pinned/reviewed dependencies.

## Next milestones
1. Historical OHLCV and corporate action data with point-in-time reproducibility.
2. Long-only momentum, trend and mean-reversion baselines; walk-forward backtests with costs.
3. Paper-only broker adapter, persistent order ledger, broker reconciliation, kill switch and sessions. See [broker adapter design](docs/BROKER_ADAPTER.md); the order ledger (`alphora/orders.py`), paper venue and order manager (`alphora/broker/`), reconciliation (`alphora/reconcile.py`), kill switch (`alphora/killswitch.py`) and offline session runner (`alphora/runner.py`) are implemented.
4. Research: FinRL deep RL, gradient boosting, news event classification and regime-aware evaluation.
5. Forward paper-trading review against SPY and simple baselines. No claim of guaranteed returns.
