// Chart-only relative volume scaling. Never infer execution liquidity from daily totals.
// A shared normalized scale keeps million-share imported bars inside the SVG.
export function normalizedVolumeHeights(candles, maxHeight = 40) {
  if (!Array.isArray(candles) || candles.length === 0 ||
      !Number.isFinite(maxHeight) || maxHeight <= 0 ||
      candles.some(bar => !bar || !Number.isSafeInteger(bar.volume) || bar.volume < 0)) {
    throw new Error("INVALID_VOLUME_CHART_DATA");
  }
  const maximum = Math.max(...candles.map(bar => bar.volume));
  return Object.freeze(candles.map(bar =>
    maximum === 0 ? 0 : maxHeight * (bar.volume / maximum)));
}
