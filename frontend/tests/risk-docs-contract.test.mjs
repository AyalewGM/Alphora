// Prevent frontend documentation from regressing to pre-#65 unsafe ceilings.
// Read-only source contract; browser and independent financial QA still required.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DEFAULT_RISK_DRAFT, validateRiskDraft } from "../src/risk-draft.js";

const doc = readFileSync(new URL("../RISK_DRAFT_PREVIEW.md", import.meta.url), "utf8");

test("risk preview docs match the five default form values and exact inert wire schema", () => {
  assert.deepEqual(DEFAULT_RISK_DRAFT, {
    startingEquityUsd: "100000", perTradePct: "0.5",
    allocationPct: "10", dailyLossPct: "2", rebalanceFrequency: "daily"
  });
  const wire = validateRiskDraft(DEFAULT_RISK_DRAFT);
  assert.deepEqual(Object.keys(wire), [
    "schema", "starting_equity_usd", "risk_per_trade_fraction",
    "max_instrument_fraction", "daily_loss_fraction", "rebalance_frequency"
  ]);
  for (const entry of [
    "Five editable proposal controls", "$100,000", "0.5%", "10%", "2%",
    "new portfolios only", "6-key", "paper-risk-proposal-v1",
    "starting_equity_usd", "risk_per_trade_fraction", "max_instrument_fraction",
    "daily_loss_fraction", "rebalance_frequency"
  ]) {
    assert.ok(doc.toLowerCase().includes(entry.toLowerCase()), entry);
  }
});

test("docs cannot reintroduce the unsafe earlier 2% trade / 100% allocation limits", () => {
  assert.doesNotMatch(doc, /up to 2% planned trade risk/i);
  assert.doesNotMatch(doc, /up to 100% instrument allocation/i);
  assert.doesNotMatch(doc, /two editable preview inputs/i);
  assert.match(doc, /at most 0\.5%/);
  assert.match(doc, /at most 10%/);
  assert.match(doc, /at most 2%/);
});

test("docs distinguish reference GET and draft preview from effective account state", () => {
  assert.match(doc, /\/v1\/risk\/policy/);
  assert.match(doc, /\/v1\/system\/capabilities/);
  assert.match(doc, /UNAVAILABLE/);
  assert.match(doc, /UNKNOWN/);
  assert.match(doc, /NOT VERIFIED/);
  assert.match(doc, /no account endpoints are implemented or called/i);
  assert.match(doc, /unattended multi-session paper execution cannot be called ready/i);
  assert.match(doc, /no settings are saved or applied/i);
  assert.match(doc, /no self-approval, auto-merge or deployment/i);
});
