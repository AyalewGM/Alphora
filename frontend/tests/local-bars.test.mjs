import test from "node:test";
import assert from "node:assert/strict";
import { parseLocalDailySnapshot } from "../src/local-bars.js";

const payload = () => ({
  symbol: "SPY", asset_type: "ETF", price_basis: "RAW_UNADJUSTED",
  source: "local_review_required", source_revision: "v1",
  sha256: "a".repeat(64), as_of: "2026-11-02T12:00:00Z",
  bars: [
    {day:"2026-10-29",open:"100",high:"105",low:"98",close:"103",volume:500,
     first_seen_at:"2026-10-29T20:01:00Z",version_available_at:"2026-10-29T20:10:00Z"},
    {day:"2026-10-30",open:"104",high:"106",low:"101",close:"105",volume:600,
     first_seen_at:"2026-10-30T20:01:00Z",version_available_at:"2026-10-30T20:10:00Z"}
  ]
});

function rejects(mutator, expected) {
  const input = payload();
  mutator(input);
  assert.throws(() => parseLocalDailySnapshot(JSON.stringify(input)),
                error => error.message === expected);
}
test("valid two-day local history can be charted but is explicitly unauthenticated", () => {
  const result = parseLocalDailySnapshot(JSON.stringify(payload()));
  assert.equal(result.symbol, "SPY");
  assert.equal(result.declared_sha256, "a".repeat(64));
  assert.deepEqual(result.bars.map(x => x.day), ["2026-10-29","2026-10-30"]);
});
test("no retrospective next-session revision is allowed", () => {
  rejects(x => x.bars[0].version_available_at = "2026-10-30T14:30:00Z", "NEXT_OPEN_LOOKAHEAD");
});
test("reject invalid OHLC and volume before render", () => {
  rejects(x => x.bars[0].low = "104", "INVALID_OHLC");
  rejects(x => x.bars[0].volume = true, "INVALID_VOLUME");
  rejects(x => x.bars[0].close = "NaN", "INVALID_DECIMAL");
});
test("reject future or pre-close data", () => {
  rejects(x => x.bars[0].first_seen_at = "2026-10-29T19:59:00Z", "PRE_CLOSE_VERSION");
  rejects(x => x.bars[1].version_available_at = "2026-11-04T12:00:00Z", "UNAVAILABLE_VERSION");
  rejects(x => x.as_of = "2099-01-01T00:00:00Z", "FUTURE_SNAPSHOT");
});
test("reject missing provenance and reordered days", () => {
  rejects(x => x.sha256 = "", "INVALID_SNAPSHOT");
  rejects(x => x.source = "", "INVALID_SNAPSHOT");
  rejects(x => x.bars[1].day = "2026-10-29", "NON_MONOTONIC_DATES");
  rejects(x => x.bars[0].day = "2026-02-30", "INVALID_DATE");
});
test("reject naive timestamps and oversized content", () => {
  rejects(x => x.bars[0].first_seen_at = "2026-10-29T20:10:00", "INVALID_TIME");
  assert.throws(() => parseLocalDailySnapshot(" ".repeat(512 * 1024 + 1)), /SNAPSHOT_TOO_LARGE/);
});
