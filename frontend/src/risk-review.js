// Read-only comparison for an inert local settings proposal. No API or state writes.
// Use exact decimal-string arithmetic so a near-ceiling input cannot be rounded away.
import { DEFAULT_RISK_DRAFT, validateRiskDraft } from "./risk-draft.js";

const BASELINE = validateRiskDraft(DEFAULT_RISK_DRAFT);

function millionthUnits(fraction) {
  if (typeof fraction !== "string" ||
      !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/.test(fraction)) {
    throw new Error("INVALID_REVIEW_FRACTION");
  }
  const [whole, fractional = ""] = fraction.split(".");
  return BigInt(whole) * 1000000n + BigInt(fractional.padEnd(6, "0") || "0");
}

function percentLabel(fraction) {
  // Six fractional digits correspond to four exact decimal places in percent.
  const units = millionthUnits(fraction);
  const whole = units / 10000n;
  const decimal = (units % 10000n).toString().padStart(4, "0").replace(/0+$/, "");
  return `${whole}${decimal ? "." + decimal : ""}%`;
}

function usdLabel(amount) {
  // Money presentation must not round through Number or strip significant
  // cents. The wire is validated before this formatter is called.
  if (typeof amount !== "string" ||
      !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(amount)) {
    throw new Error("INVALID_REVIEW_USD");
  }
  const [dollars, fraction = ""] = amount.split(".");
  const grouped = dollars.replace(/\B(?=([0-9]{3})+(?![0-9]))/g, ",");
  return `USD ${grouped}.${fraction.padEnd(2, "0")}`;
}

function ceilingRow(label, field, ceiling) {
  return Object.freeze({
    label,
    reference: `${percentLabel(BASELINE[field])} hard ceiling`,
    proposed: percentLabel(ceiling),
    meaning: millionthUnits(ceiling) < millionthUnits(BASELINE[field])
      ? "Tighter than hard ceiling" : "At approved hard ceiling"
  });
}

export function buildRiskReview(draft) {
  // The validator rejects any proposal above the hard ceilings before display.
  const wire = validateRiskDraft(draft);
  const rows = Object.freeze([
    Object.freeze({
      label: "Starting virtual equity",
      reference: `${usdLabel(BASELINE.starting_equity_usd)} (new portfolios)`,
      proposed: usdLabel(wire.starting_equity_usd),
      meaning: "New portfolios only; existing ledger unchanged"
    }),
    ceilingRow("Planned trade risk", "risk_per_trade_fraction", wire.risk_per_trade_fraction),
    ceilingRow("Instrument allocation", "max_instrument_fraction", wire.max_instrument_fraction),
    ceilingRow("Daily-loss shutdown", "daily_loss_fraction", wire.daily_loss_fraction),
    Object.freeze({
      label: "Rebalancing",
      reference: "Daily default",
      proposed: wire.rebalance_frequency,
      meaning: "Schedule proposal only; no active scheduler"
    })
  ]);
  return Object.freeze({ wire, rows });
}
