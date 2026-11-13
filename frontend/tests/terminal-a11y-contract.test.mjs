// Static accessibility / provenance guardrails for the existing React terminal.
// These contracts complement, but do not replace, manual keyboard/screen-reader testing.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const jsx = readFileSync(new URL("../src/main.jsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("the terminal provides a visible-on-focus skip link to the actual main region", () => {
  assert.match(jsx, /<a className="skip-link" href="#main-content">Skip to main content<\/a>/);
  assert.match(jsx, /<main id="main-content" className="main" tabIndex="-1">/);
  assert.match(css, /\.skip-link:focus\{top:9px;/);
});

test("nonfunctional rail glyphs are hidden and keyboard selections are announced", () => {
  assert.match(jsx, /<aside className="rail" aria-hidden="true">/);
  assert.match(jsx, /aria-pressed=\{tab===t\}/);
  assert.match(jsx, /aria-pressed=\{selected===x\.symbol\}/);
  assert.match(css, /\.watchrow:focus-visible/);
  assert.match(css, /\.tabbar button:focus-visible/);
});

test("reference API checks must not be represented as verified portfolio state", () => {
  assert.match(jsx, /Reference GET checks passed — portfolio unverified/);
  assert.match(jsx, /NO VERIFIED ACCOUNT SNAPSHOT/);
  assert.match(jsx, /<dt>Emergency halt<\/dt><dd>UNKNOWN — not attested<\/dd>/);
  assert.match(jsx, /<dt>Reconciliation<\/dt><dd>NOT VERIFIED<\/dd>/);
  assert.match(jsx, /<dt>Effective policy revision<\/dt><dd>NOT AVAILABLE<\/dd>/);
});

test("narrow viewport preserves status text and keyboard-reachable research tabs", () => {
  // Structural CSS contracts only: physical 320px browser + screen-reader QA is separate.
  const responsive = css.slice(css.indexOf("/* Responsive keyboard access:"));
  assert.match(responsive, /@media \(max-width:560px\)/);
  assert.match(responsive, /\.topbar \{ height:auto; min-height:66px; flex-wrap:wrap;/);
  assert.match(responsive, /\.top-right \[role="status"\] \{ min-width:0; overflow-wrap:anywhere;/);
  assert.match(responsive, /\.tabbar \{ overflow-x:auto; white-space:nowrap;/);
  assert.match(responsive, /\.tabbar button \{ flex:0 0 auto; min-height:44px/);
  assert.match(jsx, /<span role="status" aria-live="polite">\{apiState\}<\/span>/);
});
