# 2023 GitLab trader archive → Alphora migration assessment

Source: user-provided `trader-main.zip` from private `gitlab.com/Ayalew/trader`, most recent original commit `f4522d10` (December 2023).

## Preserved as research knowledge, not copied runtime
- FinRL's research approach: financial feature engineering, A2C/PPO/DDPG ensemble experiments, turbulence/regime analysis, walk-forward rebalancing.
- US stocks universe and brokerage-adapter concepts.
- Container-first development idea and isolated unit tests.
- Alpaca paper trading as an eventual *separate, explicitly paper-only* adapter.

## Explicitly excluded
- The Flask routes in `app.py`, `app_new.py`, and `app_new_2.py` read a caller-supplied paper toggle and invoke `TradingClient(..., paper=paper_trading)`; this can select live mode. They also accept credentials from web forms. **Never deploy or import these routes.**
- `trading_bot.py` contains a `paper=False` client path and import-time expensive training/data calls.
- `app_new_2.py` contains placeholders (`your_dataframe`, `...`) and nonworking/unverified imports.
- The original Poetry project targets Python ~3.8 and bundles a third-party FinRL version 0.3.6; do not overwrite it with an unreviewed mass dependency upgrade.
- The old demonstration prints API keys; never migrate it into runnable examples.
- Large third-party fork, notebooks, illustrations and generated research artifacts: not copied. Instead depend on a separately pinned, reviewed FinRL version in a future optional research environment, preserving MIT attribution.

## Current safe foundation
- New `alphora/risk.py`: pure paper-only decision guard.
- `tests/test_risk.py`: deterministic rejection/boundary tests.
- No broker connection or execution path exists.

## Before professional-grade paper trading
- Immutable risk audit and order ledger, crash recovery, idempotency, partial fills, broker-side reconciliation.
- Persisted daily-loss halts, corporate actions, market calendar, session accounting and mark-to-market across positions.
- Point-in-time datasets, survivorship-bias controls, transaction cost/latency simulation and walk-forward experiments.
- Model registry, training/test isolation, and documented benchmark comparisons.
- Strict dependency pinning, security scanning, CI and operational alerting.

**Research performance is unknown. No claim of predictive advantage or production readiness is made.**
