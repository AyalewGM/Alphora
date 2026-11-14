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
    visited.push([url, init.method, init.credentials, init.redirect, init.cache]);
    return {ok:true, json:async()=> url.includes("policy") ? policy : capabilities};
  };
  const result = await loadLocalReadOnlyStatus(fetcher);
  assert.equal(result.capabilities.paper_orders_enabled, false);
  assert.deepEqual(visited, [
    ["/v1/risk/policy","GET","omit","error","no-store"],
    ["/v1/system/capabilities","GET","omit","error","no-store"]
  ]);
});

test("redirected or cached-reference fetches cannot silently attest local-only status", async () => {
  // redirect:error prevents browser fetch from following 30x to an off-origin
  // site. A supplied fetcher which reports a followed redirect is also rejected.
  let calls = 0;
  const followedRedirect = async (path, init) => {
    calls++;
    assert.equal(init.redirect, "error");
    assert.equal(init.cache, "no-store");
    return {
      ok: true, redirected: path.includes("policy"),
      json: async () => path.includes("policy") ? policy : capabilities
    };
  };
  await assert.rejects(loadLocalReadOnlyStatus(followedRedirect),
    /LOCAL_API_UNAVAILABLE/);
  assert.equal(calls, 2);
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

test("reference GET responses reject unknown fields, conflicting execution hints and non-JSON shapes", () => {
  // These two endpoints attest only a fixed, inert reference schema. Unknown
  // fields must not be silently treated as verified capabilities or account
  // state, particularly if a future backend adds a contradictory order flag.
  for (const extra of [
    { paper_orders_enabled: true },
    { effective_policy_revision: 9 },
    { clear_halt: true }
  ]) {
    assert.throws(() => validateBackendStatus({...policy, ...extra}, capabilities),
      /UNSAFE_OR_UNVERIFIED_CAPABILITIES/);
  }
  for (const extra of [
    { broker_orders_enabled: true },
    { execution_mode: "live" },
    { connected_broker: "unknown" }
  ]) {
    assert.throws(() => validateBackendStatus(policy, {...capabilities, ...extra}),
      /UNSAFE_OR_UNVERIFIED_CAPABILITIES/);
  }
  const missingPolicy = {...policy};
  delete missingPolicy.max_instrument_allocation_fraction;
  assert.throws(() => validateBackendStatus(missingPolicy, capabilities),
    /UNSAFE_OR_UNVERIFIED_CAPABILITIES/);
  const missingCapability = {...capabilities};
  delete missingCapability.live_orders_enabled;
  assert.throws(() => validateBackendStatus(policy, missingCapability),
    /UNSAFE_OR_UNVERIFIED_CAPABILITIES/);
  for (const malformed of [[], Object.create(null),
     JSON.parse('{"market":["US_STOCKS","US_ETFS"],"research_enabled":true,"paper_orders_enabled":false,"live_orders_enabled":false,"rebalance_frequency":"daily","__proto__":{}}')]) {
    assert.throws(() => validateBackendStatus(policy, malformed),
      /UNSAFE_OR_UNVERIFIED_CAPABILITIES/);
  }
});
