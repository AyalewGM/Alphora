import test from "node:test";
import assert from "node:assert/strict";
import { normalizedVolumeHeights } from "../src/chart-scale.js";

test("million-share imported volumes fit within the fixed chart strip", () => {
  const heights = normalizedVolumeHeights([
    { volume: 1_000_000 }, { volume: 2_000_000 }, { volume: 0 }
  ], 40);
  assert.deepEqual(heights, [20, 40, 0]);
  assert.ok(heights.every(height => height >= 0 && height <= 40));
});

test("relative volumes stay proportional and zero-volume bars stay invisible", () => {
  const bars = [{volume: 1}, {volume: 8_000_000}, {volume: 0}];
  assert.deepEqual(normalizedVolumeHeights(bars, 32), [0.000004, 32, 0]);
  assert.deepEqual(normalizedVolumeHeights([{volume:0},{volume:0}]), [0,0]);
  assert.deepEqual(bars.map(x => x.volume), [1, 8_000_000, 0]);
});

test("invalid heights and invalid volumes fail closed before chart drawing", () => {
  for (const bars of [[], [{volume:-1}], [{volume:1.5}],
                     [{volume:Infinity}], [{volume:true}], [null]]) {
    assert.throws(() => normalizedVolumeHeights(bars), /INVALID_VOLUME_CHART_DATA/);
  }
  for (const height of [0,-1,Infinity,NaN]) {
    assert.throws(() => normalizedVolumeHeights([{volume:3}],height),
                  /INVALID_VOLUME_CHART_DATA/);
  }
});
