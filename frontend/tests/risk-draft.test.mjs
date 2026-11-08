import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_RISK_DRAFT, validateRiskDraft } from "../src/risk-draft.js";

test("approved baseline remains the offline draft default", () => {
  assert.deepEqual(DEFAULT_RISK_DRAFT, { perTradePct: "0.5", allocationPct: "10" });
  const result = validateRiskDraft(DEFAULT_RISK_DRAFT);
  assert.equal(result.max_planned_trade_risk_fraction, "0.005000");
  assert.equal(result.max_instrument_allocation_fraction, "0.100000");
  assert.equal(result.daily_loss_shutdown_fraction, "0.02");
  assert.equal(result.status, "PREVIEW_ONLY_NOT_APPLIED");
  assert.equal(Object.isFrozen(result), true);
});

test("tighter and higher validated settings do not mutate approved defaults", () => {
  const conservative = validateRiskDraft({ perTradePct: "0.25", allocationPct: "5" });
  assert.equal(conservative.max_planned_trade_risk_fraction, "0.002500");
  assert.equal(conservative.max_instrument_allocation_fraction, "0.050000");
  const higher = validateRiskDraft({ perTradePct: "1.25", allocationPct: "20" });
  assert.equal(higher.max_planned_trade_risk_fraction, "0.012500");
  assert.equal(higher.max_instrument_allocation_fraction, "0.200000");
  assert.equal(DEFAULT_RISK_DRAFT.perTradePct, "0.5");
  assert.equal(DEFAULT_RISK_DRAFT.allocationPct, "10");
});

test("absolute bounds are checked as exact decimals", () => {
  const boundary = validateRiskDraft({ perTradePct: "2.0000", allocationPct: "100" });
  assert.equal(boundary.max_planned_trade_risk_fraction, "0.020000");
  assert.equal(boundary.max_instrument_allocation_fraction, "1.000000");
  for (const bad of [
    { perTradePct: "2.0001", allocationPct: "10" },
    { perTradePct: "0", allocationPct: "10" },
    { perTradePct: "0.5", allocationPct: "100.0001" },
    { perTradePct: "0.5", allocationPct: "0" }
  ]) {
    assert.throws(() => validateRiskDraft(bad), /PERCENT_OUT_OF_BOUNDS/);
  }
});

test("malformed and ambiguous inputs fail closed, including scientific notation", () => {
  for (const bad of ["", " 0.5", "0.5 ", ".5", "1e-1", "-1", "+1", "01", "0.12345", "NaN", "Infinity", "5%", "0,5", 0.5, null]) {
    assert.throws(() => validateRiskDraft({perTradePct:bad, allocationPct:"10"}), /INVALID_PERCENT_FORMAT/);
  }
  assert.throws(() => validateRiskDraft(null), /INVALID_RISK_DRAFT/);
  assert.throws(() => validateRiskDraft({perTradePct:"0.5"}), /INVALID_PERCENT_FORMAT/);
});

test("display preview never returns capability, order or activation flags", () => {
  const preview = validateRiskDraft({perTradePct:"0.5", allocationPct:"10"});
  for (const forbidden of ["paper_orders_enabled", "live_orders_enabled", "submit_order", "policy_active", "mode"]) {
    assert.equal(Object.hasOwn(preview, forbidden), false);
  }
});
