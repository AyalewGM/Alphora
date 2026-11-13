import test from "node:test";
import assert from "node:assert/strict";
import { APPROVED_POLICY, validateBackendStatus, loadLocalReadOnlyStatus } from "../src/api.js";

const policy = { mode: "paper", ...APPROVED_POLICY };
const capabilities = {
  market: ["US_STOCKS", "US_ETFS"], research_enabled: true,
  paper_orders_enabled: false, live_orders_enabled: false,
  rebalance_frequency: "daily"
};

test("validated paper-only policy and capabilities can be displayed", () => {
  const result = validateBackendStatus(policy, capabilities);
  assert.equal(result.policy.starting_virtual_equity, "100000");
  assert.equal(result.capabilities.live_orders_enabled, false);
});

test("live and paper execution flags fail closed", () => {
  for (const change of [{live_orders_enabled:true}, {paper_orders_enabled:true},
                        {research_enabled:false}, {market:["US_STOCKS"]}]) {
    assert.throws(() => validateBackendStatus(policy, {...capabilities,...change}));
  }
});

test("risk settings cannot be weakened or interpreted from floats", () => {
  for (const change of [
    {mode:"live"}, {starting_virtual_equity:"200000"},
    {max_planned_trade_risk_fraction:"0.01"},
    {max_instrument_allocation_fraction:"0.2"},
    {daily_loss_shutdown_fraction:"0.04"},
    {max_planned_trade_risk_fraction:0.005},
    {max_planned_trade_risk_fraction:"0.0050000000000000000000001"},
    {max_instrument_allocation_fraction:"0.10000000000000000000001"},
    {daily_loss_shutdown_fraction:"0.020000000000000000000001"},
    {max_planned_trade_risk_fraction:"00.005"},
    {rebalance_frequency:"weekly"}
  ]) {
    assert.throws(() => validateBackendStatus({...policy,...change},capabilities));
  }
});

test("local GET-only API uses exactly two same-origin endpoints", async () => {
  const visited = [];
  const fetcher = async (url, init) => {
    visited.push([url, init.method, init.credentials]);
    return {ok:true, json:async()=> url.includes("policy") ? policy : capabilities};
  };
  const result = await loadLocalReadOnlyStatus(fetcher);
  assert.equal(result.capabilities.paper_orders_enabled, false);
  assert.deepEqual(visited, [
    ["/v1/risk/policy","GET","omit"],
    ["/v1/system/capabilities","GET","omit"]
  ]);
});

test("HTTP failures and invalid JSON contracts do not establish connection", async () => {
  await assert.rejects(loadLocalReadOnlyStatus(async () => ({ok:false})));
  await assert.rejects(loadLocalReadOnlyStatus(async () => ({ok:true,json:async()=>({})})));
});

test("read-only risk validation accepts exact numerically equivalent decimal strings", () => {
  const equivalent = {
    ...policy,
    starting_virtual_equity:"100000.00",
    max_planned_trade_risk_fraction:"0.00500",
    max_instrument_allocation_fraction:"0.1000",
    daily_loss_shutdown_fraction:"0.02000"
  };
  const result = validateBackendStatus(equivalent, capabilities);
  assert.equal(result.policy.max_planned_trade_risk_fraction, "0.005");
  assert.equal(result.policy.starting_virtual_equity, "100000");
});
