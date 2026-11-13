import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_RISK_DRAFT, validateRiskDraft } from "../src/risk-draft.js";

test("default proposal matches backend paper-risk-proposal-v1 wire fields", () => {
  assert.deepEqual(DEFAULT_RISK_DRAFT, {
    startingEquityUsd: "100000", perTradePct: "0.5", allocationPct: "10",
    dailyLossPct: "2", rebalanceFrequency: "daily"
  });
  const result = validateRiskDraft(DEFAULT_RISK_DRAFT);
  assert.deepEqual(result, {
    schema: "paper-risk-proposal-v1",
    starting_equity_usd: "100000",
    risk_per_trade_fraction: "0.005",
    max_instrument_fraction: "0.1",
    daily_loss_fraction: "0.02",
    rebalance_frequency: "daily"
  });
  assert.equal(Object.isFrozen(result), true);
});

test("tighter settings and new-portfolio capital produce only inert proposals", () => {
  const result = validateRiskDraft({
    startingEquityUsd: "25000.50", perTradePct: "0.25",
    allocationPct: "5", dailyLossPct: "1", rebalanceFrequency: "weekly"
  });
  assert.equal(result.starting_equity_usd, "25000.5");
  assert.equal(result.risk_per_trade_fraction, "0.0025");
  assert.equal(result.max_instrument_fraction, "0.05");
  assert.equal(result.daily_loss_fraction, "0.01");
  assert.equal(result.rebalance_frequency, "weekly");
  const manual = validateRiskDraft({...DEFAULT_RISK_DRAFT, rebalanceFrequency: "manual"});
  assert.equal(manual.rebalance_frequency, "manual");
  assert.equal(DEFAULT_RISK_DRAFT.startingEquityUsd, "100000");
  assert.equal(DEFAULT_RISK_DRAFT.rebalanceFrequency, "daily");
});

test("hard owner ceilings cannot be loosened even when preview only", () => {
  const cases = [
    {perTradePct:"0.5001"}, {perTradePct:"2"},
    {allocationPct:"10.0001"}, {allocationPct:"100"},
    {dailyLossPct:"2.0001"}, {dailyLossPct:"10"},
    {perTradePct:"0"}, {allocationPct:"0"}, {dailyLossPct:"0"}
  ];
  for (const change of cases) {
    assert.throws(() => validateRiskDraft({...DEFAULT_RISK_DRAFT,...change}),
      /PERCENT_OUT_OF_BOUNDS/, JSON.stringify(change));
  }
  assert.equal(validateRiskDraft(DEFAULT_RISK_DRAFT).daily_loss_fraction, "0.02");
});

test("exact formatting, USD cents and strict schema reject ambiguous input", () => {
  for (const input of ["", " 0.5", "0.5 ", ".5", "1e-1", "-1", "+1",
                       "01", "0.12345", "NaN", "Infinity", "5%", "0,5", 0.5, null]) {
    assert.throws(() => validateRiskDraft({...DEFAULT_RISK_DRAFT,perTradePct:input}),
      /INVALID_PERCENT_FORMAT/);
  }
  for (const input of ["", "0", "0.00", "-1", "01", "100000.001",
                       "1e5", "NaN", "Infinity", " 100", 100, null, "9".repeat(65)]) {
    assert.throws(() => validateRiskDraft({...DEFAULT_RISK_DRAFT,startingEquityUsd:input}),
      /INVALID_STARTING_EQUITY/);
  }
  assert.equal(validateRiskDraft({...DEFAULT_RISK_DRAFT,startingEquityUsd:"100000.00"}).starting_equity_usd, "100000");
  assert.equal(validateRiskDraft({...DEFAULT_RISK_DRAFT,startingEquityUsd:"0.01"}).starting_equity_usd, "0.01");
  assert.equal(validateRiskDraft({...DEFAULT_RISK_DRAFT,perTradePct:"0.5000"}).risk_per_trade_fraction,"0.005");
});

test("unknown cadence, unrecognized or partial settings fail closed", () => {
  for (const frequency of ["hourly", "DAILY", "", null, true]) {
    assert.throws(() => validateRiskDraft({...DEFAULT_RISK_DRAFT,rebalanceFrequency:frequency}),
      /INVALID_REBALANCE_FREQUENCY/);
  }
  assert.throws(() => validateRiskDraft(null), /INVALID_RISK_DRAFT/);
  assert.throws(() => validateRiskDraft({...DEFAULT_RISK_DRAFT,live_orders_enabled:true}),
    /INVALID_RISK_DRAFT/);
  assert.throws(() => validateRiskDraft({perTradePct:"0.5"}), /INVALID_RISK_DRAFT/);
  assert.throws(() => validateRiskDraft([]), /INVALID_RISK_DRAFT/);
});

test("wire proposal never contains execution, approval, halt-clear or persistence switches", () => {
  const preview = validateRiskDraft(DEFAULT_RISK_DRAFT);
  for (const forbidden of ["paper_orders_enabled","live_orders_enabled","mode",
       "submit_order","policy_active","clear_halt","override_reconciliation",
       "revision","applied_at"]) {
    assert.equal(Object.hasOwn(preview,forbidden), false, forbidden);
  }
});
