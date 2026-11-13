// Read-only local backend contract. Never retrieves orders or market prices.
export const APPROVED_POLICY = Object.freeze({
  starting_virtual_equity: "100000",
  max_planned_trade_risk_fraction: "0.005",
  max_instrument_allocation_fraction: "0.10",
  daily_loss_shutdown_fraction: "0.02",
  rebalance_frequency: "daily"
});
export const OFFLINE_CAPABILITIES = Object.freeze({
  research_enabled: false, paper_orders_enabled: false,
  live_orders_enabled: false
});

function canonicalDecimal(value) {
  // Exact comparison with the read-only policy: Number() would round an
  // infinitesimally looser limit to the approved float and wrongly accept it.
  if (typeof value !== "string" || value.length > 64 ||
      !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const significant = fraction.replace(/0+$/, "");
  return significant ? whole + "." + significant : whole;
}

export function validateBackendStatus(policy, capabilities) {
  if (!policy || !capabilities || policy.mode !== "paper" ||
      capabilities.research_enabled !== true ||
      capabilities.paper_orders_enabled !== false ||
      capabilities.live_orders_enabled !== false ||
      capabilities.rebalance_frequency !== "daily" ||
      !Array.isArray(capabilities.market) ||
      capabilities.market.length !== 2 ||
      !["US_STOCKS", "US_ETFS"].every(x => capabilities.market.includes(x))) {
    throw new Error("UNSAFE_OR_UNVERIFIED_CAPABILITIES");
  }
  for (const key of ["starting_virtual_equity", "max_planned_trade_risk_fraction",
                     "max_instrument_allocation_fraction", "daily_loss_shutdown_fraction"]) {
    if (canonicalDecimal(policy[key]) === null ||
        canonicalDecimal(policy[key]) !== canonicalDecimal(APPROVED_POLICY[key])) {
      throw new Error("UNEXPECTED_RISK_POLICY");
    }
  }
  if (policy.rebalance_frequency !== APPROVED_POLICY.rebalance_frequency) {
    throw new Error("UNEXPECTED_RISK_POLICY");
  }
  return {
    policy: Object.freeze({ ...APPROVED_POLICY }),
    capabilities: Object.freeze({
      research_enabled: true, paper_orders_enabled: false,
      live_orders_enabled: false
    })
  };
}

export async function loadLocalReadOnlyStatus(fetcher, signal) {
  // Same-origin, GET-only calls to the local FastAPI shell.
  const paths = ["/v1/risk/policy", "/v1/system/capabilities"];
  const responses = await Promise.all(paths.map(path =>
    fetcher(path, { method: "GET", credentials: "omit", signal })));
  if (responses.some(r => !r.ok)) throw new Error("LOCAL_API_UNAVAILABLE");
  const [policy, capabilities] = await Promise.all(responses.map(r => r.json()));
  return validateBackendStatus(policy, capabilities);
}
