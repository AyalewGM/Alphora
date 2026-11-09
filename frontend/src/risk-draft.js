// Offline-only operator draft validation. Does not apply a policy to the backend,
// persist configuration, issue orders, or alter the read-only API contract.
// Percent values are parsed with decimal strings and BigInt, never floats.
const PERCENT = /^(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,4})?$/;

export const DEFAULT_RISK_DRAFT = Object.freeze({
  perTradePct: "0.5",
  allocationPct: "10"
});

function parsePercent(value, maximumUnits) {
  if (typeof value !== "string" || !PERCENT.test(value)) {
    throw new Error("INVALID_PERCENT_FORMAT");
  }
  const [whole, fractional = ""] = value.split(".");
  const units = BigInt(whole) * 10000n + BigInt(fractional.padEnd(4, "0") || "0");
  if (units <= 0n || units > maximumUnits) {
    throw new Error("PERCENT_OUT_OF_BOUNDS");
  }
  return units;
}

function fractionString(units) {
  // A percent with four decimal places becomes a fraction with six places.
  const scaled = units.toString().padStart(7, "0");
  return `${scaled.slice(0, -6)}.${scaled.slice(-6)}`;
}

export function validateRiskDraft(draft) {
  if (!draft || typeof draft !== "object" || Array.isArray(draft)) {
    throw new Error("INVALID_RISK_DRAFT");
  }
  const perTradeUnits = parsePercent(draft.perTradePct, 20000n); // 2%
  const allocationUnits = parsePercent(draft.allocationPct, 1000000n); // 100%
  return Object.freeze({
    max_planned_trade_risk_fraction: fractionString(perTradeUnits),
    max_instrument_allocation_fraction: fractionString(allocationUnits),
    daily_loss_shutdown_fraction: "0.02",
    starting_virtual_equity: "100000",
    rebalance_frequency: "daily",
    status: "PREVIEW_ONLY_NOT_APPLIED"
  });
}
