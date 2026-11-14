import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_RISK_DRAFT } from "../src/risk-draft.js";
import { buildRiskReview } from "../src/risk-review.js";

test("approved defaults compare as ceilings without claiming an effective policy", () => {
  const review = buildRiskReview(DEFAULT_RISK_DRAFT);
  assert.equal(review.wire.schema, "paper-risk-proposal-v1");
  assert.deepEqual(review.rows.map(row => row.label), [
    "Starting virtual equity", "Planned trade risk",
    "Instrument allocation", "Daily-loss shutdown", "Rebalancing"
  ]);
  assert.deepEqual(review.rows.slice(1, 4).map(row => row.proposed), [
    "0.5%", "10%", "2%"
  ]);
  assert.ok(review.rows.slice(1, 4).every(row => row.meaning === "At approved hard ceiling"));
  assert.equal(review.rows[0].reference, "USD 100,000.00 (new portfolios)");
  assert.equal(review.rows[0].meaning, "New portfolios only; existing ledger unchanged");
  assert.equal(review.rows[4].meaning, "Schedule proposal only; no active scheduler");
  assert.equal(Object.isFrozen(review), true);
  assert.equal(Object.isFrozen(review.rows), true);
  assert.equal(Object.isFrozen(review.rows[1]), true);
});

test("tightened proposals display exact percentages without floating point rounding", () => {
  const review = buildRiskReview({
    ...DEFAULT_RISK_DRAFT,
    startingEquityUsd: "25000.50",
    perTradePct: "0.0001",
    allocationPct: "5.0001",
    dailyLossPct: "1.9999",
    rebalanceFrequency: "manual"
  });
  assert.equal(review.rows[0].proposed, "USD 25,000.50");
  assert.deepEqual(review.rows.slice(1, 4).map(row => row.proposed), [
    "0.0001%", "5.0001%", "1.9999%"
  ]);
  assert.ok(review.rows.slice(1, 4).every(row => row.meaning === "Tighter than hard ceiling"));
  assert.equal(review.rows[4].proposed, "manual");
  assert.deepEqual(review.rows.slice(1, 4).map(row => row.reference), [
    "0.5% hard ceiling", "10% hard ceiling", "2% hard ceiling"
  ]);
});

test("comparison fails closed on loosened, malformed or incomplete proposals", () => {
  for (const change of [
    { perTradePct: "0.5001" },
    { allocationPct: "10.0001" },
    { dailyLossPct: "2.0001" },
    { perTradePct: "NaN" },
    { startingEquityUsd: "-5" },
    { rebalanceFrequency: "hourly" },
    { clearHalt: true }
  ]) {
    assert.throws(() => buildRiskReview({ ...DEFAULT_RISK_DRAFT, ...change }),
      undefined, JSON.stringify(change));
  }
  assert.throws(() => buildRiskReview({}), /INVALID_RISK_DRAFT/);
  for (const name of ["enabled","active","clear_halt","revision","apply","execute","orders"]) {
    assert.equal(Object.hasOwn(buildRiskReview(DEFAULT_RISK_DRAFT), name), false, name);
  }
});


test("USD proposal comparison preserves cents and huge integers without Number rounding", () => {
  for (const [startingEquityUsd, label] of [
    ["0.01", "USD 0.01"],
    ["1000", "USD 1,000.00"],
    ["999999999999999999999999999999999999.99",
     "USD 999,999,999,999,999,999,999,999,999,999,999,999.99"]
  ]) {
    const review = buildRiskReview({ ...DEFAULT_RISK_DRAFT, startingEquityUsd });
    assert.equal(review.rows[0].proposed, label);
    // Presentation only; retain the canonical exact wire value.
    assert.equal(review.wire.starting_equity_usd, startingEquityUsd);
  }
});
