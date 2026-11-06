// Client-side validation of manually supplied historical daily bars.
// This is NOT a vendor-origin authenticator or a trade-eligibility gate.
const SYMBOL = /^[A-Z][A-Z0-9.-]{0,9}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const ISO_OFFSET = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const SHA = /^[0-9a-f]{64}$/;
const DECIMAL = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const BASES = new Set(["RAW_UNADJUSTED", "SPLIT_ADJUSTED", "TOTAL_RETURN_ADJUSTED"]);
const TYPES = new Set(["STOCK", "ETF"]);
const MAX_BYTES = 512 * 1024;
const MAX_BARS = 3000;
const est = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit",
  day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
});

function nyParts(instant) {
  const parts = Object.fromEntries(est.formatToParts(instant).map(p => [p.type, p.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}`,
           minutes: Number(parts.hour) * 60 + Number(parts.minute) };
}
function instant(text) {
  if (typeof text !== "string" || !ISO_OFFSET.test(text)) throw new Error("INVALID_TIME");
  const value = new Date(text);
  if (!Number.isFinite(value.getTime())) throw new Error("INVALID_TIME");
  return value;
}
function amount(value) {
  if (typeof value !== "string" || !DECIMAL.test(value)) throw new Error("INVALID_DECIMAL");
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) throw new Error("INVALID_DECIMAL");
  return n;
}
function date(value) {
  if (typeof value !== "string" || !DAY.test(value) ||
      !Number.isFinite(Date.parse(value + "T00:00:00Z")) ||
      new Date(value + "T00:00:00Z").toISOString().slice(0, 10) !== value) {
    throw new Error("INVALID_DATE");
  }
  return value;
}
export function parseLocalDailySnapshot(text) {
  if (typeof text !== "string" || new TextEncoder().encode(text).byteLength > MAX_BYTES) {
    throw new Error("SNAPSHOT_TOO_LARGE");
  }
  let raw;
  try { raw = JSON.parse(text); } catch { throw new Error("INVALID_JSON"); }
  if (!raw || Array.isArray(raw) || typeof raw !== "object") throw new Error("INVALID_SNAPSHOT");
  if (!SYMBOL.test(raw.symbol ?? "") || !TYPES.has(raw.asset_type) ||
      !BASES.has(raw.price_basis) || typeof raw.source !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(raw.source) ||
      typeof raw.source_revision !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(raw.source_revision) ||
      typeof raw.sha256 !== "string" || !SHA.test(raw.sha256) ||
      !Array.isArray(raw.bars) || raw.bars.length < 2 || raw.bars.length > MAX_BARS) {
    throw new Error("INVALID_SNAPSHOT");
  }
  const asOf = instant(raw.as_of);
  if (asOf > new Date()) throw new Error("FUTURE_SNAPSHOT");
  const bars = [];
  let previousDay = "";
  let previousVersion;
  for (const item of raw.bars) {
    if (!item || Array.isArray(item) || typeof item !== "object") throw new Error("INVALID_BAR");
    const day = date(item.day);
    if (day <= previousDay) throw new Error("NON_MONOTONIC_DATES");
    if (!Number.isSafeInteger(item.volume) || item.volume < 0) throw new Error("INVALID_VOLUME");
    const open = amount(item.open), high = amount(item.high);
    const low = amount(item.low), close = amount(item.close);
    if (low > Math.min(open, close, high) || high < Math.max(open, close, low)) {
      throw new Error("INVALID_OHLC");
    }
    const firstSeen = instant(item.first_seen_at);
    const available = instant(item.version_available_at);
    if (available < firstSeen || available > asOf) throw new Error("UNAVAILABLE_VERSION");
    const seenNY = nyParts(firstSeen);
    if (seenNY.day < day || (seenNY.day === day && seenNY.minutes < 16 * 60)) {
      throw new Error("PRE_CLOSE_VERSION");
    }
    if (previousVersion) {
      // Compare the previous version's known NY wall-clock against the next dated bar's 09:30 session open.
      const prevNY = nyParts(previousVersion);
      if (prevNY.day > day || (prevNY.day === day && prevNY.minutes >= 9 * 60 + 30)) {
        throw new Error("NEXT_OPEN_LOOKAHEAD");
      }
    }
    bars.push({day, open, high, low, close, volume:item.volume});
    previousDay = day;
    previousVersion = available;
  }
  return Object.freeze({
    symbol: raw.symbol, asset_type: raw.asset_type,
    price_basis: raw.price_basis, source:raw.source,
    source_revision:raw.source_revision, as_of:raw.as_of,
    declared_sha256:raw.sha256, bars:Object.freeze(bars)
  });
}
