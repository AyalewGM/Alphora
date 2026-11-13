// Browser-only configuration proposal: never saved, submitted or activated.
// Mirrors the inert Python PaperRiskConfig.to_wire() schema merged in PR #66.
// These are OWNER-APPROVED HARD CEILINGS, not user-adjustable maximums.
const PERCENT = /^(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,4})?$/;
const USD = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/;
const CADENCES = Object.freeze(["daily", "weekly", "manual"]);
const DRAFT_KEYS = Object.freeze([
  "startingEquityUsd", "perTradePct", "allocationPct",
  "dailyLossPct", "rebalanceFrequency"
]);
const SCHEMA = "paper-risk-proposal-v1";

export const DEFAULT_RISK_DRAFT = Object.freeze({
  startingEquityUsd: "100000",
  perTradePct: "0.5",
  allocationPct: "10",
  dailyLossPct: "2",
  rebalanceFrequency: "daily"
});

function invalid(code, field) {
  const error = new Error(code);
  error.field = field;
  throw error;
}

export function describeRiskDraftError(error) {
  switch (error?.message) {
    case "PERCENT_OUT_OF_BOUNDS":
      return "Enter a value above 0 within the approved ceilings: 0.5% trade risk, 10% allocation, 2% daily loss.";
    case "INVALID_PERCENT_FORMAT":
      return "Enter a nonnegative percentage using digits and up to four decimal places, without a percent sign.";
    case "INVALID_STARTING_EQUITY":
      return "Enter positive USD starting capital using at most two decimal places; it applies to new portfolios only.";
    case "INVALID_REBALANCE_FREQUENCY":
      return "Choose Daily, Weekly or Manual. Scheduling is not yet active.";
    default:
      return "The draft is incomplete or unsupported. Reset to approved defaults and try again.";
  }
}

function parsePercent(value, maximumUnits, field) {
  if (typeof value !== "string" || !PERCENT.test(value)) {
    invalid("INVALID_PERCENT_FORMAT", field);
  }
  // Four decimal places in the percent, represented as BigInt basis points
  // of basis points; no JS float parsing, rounding or scientific notation.
  const [whole, fractional = ""] = value.split(".");
  const units = BigInt(whole) * 10000n + BigInt(fractional.padEnd(4, "0") || "0");
  if (units <= 0n || units > maximumUnits) invalid("PERCENT_OUT_OF_BOUNDS", field);
  return units;
}

function fractionString(percentUnits) {
  // 1% = 0.01; 4 percent-decimal places yield 6 fraction-decimal places.
  const padded = percentUnits.toString().padStart(7, "0");
  const whole = padded.slice(0, -6);
  const fractional = padded.slice(-6).replace(/0+$/, "");
  return fractional ? whole + "." + fractional : whole;
}

function newPortfolioEquity(value) {
  // The backend proposal wire contract accepts decimal strings <= 64 chars
  // and only whole cents. This never changes an existing ledger's balance.
  if (typeof value !== "string" || value.length > 64 || !USD.test(value)) {
    invalid("INVALID_STARTING_EQUITY", "startingEquityUsd");
  }
  const [whole, cents = ""] = value.split(".");
  const scaled = BigInt(whole) * 100n + BigInt(cents.padEnd(2, "0") || "0");
  if (scaled <= 0n) invalid("INVALID_STARTING_EQUITY", "startingEquityUsd");
  // Canonical decimal spelling: matches Python Decimal format / trim rules.
  const centFraction = cents.replace(/0+$/, "");
  return centFraction ? whole + "." + centFraction : whole;
}

export function validateRiskDraft(draft) {
  if (!draft || typeof draft !== "object" || Array.isArray(draft) ||
      Object.getPrototypeOf(draft) !== Object.prototype ||
      Object.keys(draft).sort().join("|") !== [...DRAFT_KEYS].sort().join("|")) {
    throw new Error("INVALID_RISK_DRAFT");
  }
  if (typeof draft.rebalanceFrequency !== "string" ||
      !CADENCES.includes(draft.rebalanceFrequency)) {
    invalid("INVALID_REBALANCE_FREQUENCY", "rebalanceFrequency");
  }
  // Never offer higher paper limits than the existing hard safety ceiling:
  // 0.5% planned trade risk, 10% per instrument and 2% daily loss halt.
  const perTradeUnits = parsePercent(draft.perTradePct, 5000n, "perTradePct");
  const allocationUnits = parsePercent(draft.allocationPct, 100000n, "allocationPct");
  const dailyLossUnits = parsePercent(draft.dailyLossPct, 20000n, "dailyLossPct");
  return Object.freeze({
    schema: SCHEMA,
    starting_equity_usd: newPortfolioEquity(draft.startingEquityUsd),
    risk_per_trade_fraction: fractionString(perTradeUnits),
    max_instrument_fraction: fractionString(allocationUnits),
    daily_loss_fraction: fractionString(dailyLossUnits),
    rebalance_frequency: draft.rebalanceFrequency
  });
}
