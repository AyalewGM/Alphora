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


test("paper account tables distinguish unavailable evidence from empty exposure or order history", () => {
  assert.match(jsx, /<h3 id="monitor-positions-title">Positions and pending exposure<\/h3>/);
  assert.match(jsx, /<h3 id="monitor-journal-title">Paper decision journal<\/h3>/);
  assert.match(jsx, /UNAVAILABLE — NOT ZERO POSITIONS/);
  assert.match(jsx, /NOT VERIFIED — NOT AN EMPTY JOURNAL/);
  assert.match(jsx, /This does not prove no orders or fills occurred/);
  assert.match(jsx, /<th scope="col">Pending exposure<\/th>/);
  assert.match(jsx, /<th scope="col">Risk &amp; reconciliation<\/th>/);
  assert.match(jsx, /role="region" aria-label="Positions and pending exposure table, horizontally scrollable" tabIndex="0"/);
  assert.match(jsx, /role="region" aria-label="Decision journal table, horizontally scrollable" tabIndex="0"/);
  assert.match(css, /\.monitor-table-scroll\{max-width:100%;overflow-x:auto;/);
  assert.match(css, /\.monitor-table-scroll:focus-visible\{outline:3px/);
  // The monitor must not imply that placeholders attest actual holdings or fills.
  assert.doesNotMatch(jsx, /No open positions|No orders (?:have been )?placed|Halt is clear/);
});

test("validated settings expose only an accessible, un-applied comparison, not effective policy", () => {
  assert.match(jsx, /setRiskPreview\(buildRiskReview\(riskDraft\)\)/);
  assert.match(jsx, /Validated local preview — NOT APPLIED/);
  assert.match(jsx, /not the account's effective policy/);
  assert.match(jsx, /<th scope="col">Approved reference<\/th>/);
  assert.match(jsx, /<th scope="col">Local proposal<\/th>/);
  assert.match(jsx, /riskPreview\.rows\.map\(row =>/);
  assert.match(jsx, /role="region" aria-label="Draft risk comparison table, horizontally scrollable" tabIndex="0"/);
  assert.match(jsx, /Nothing was saved, sent, activated or scheduled/);
  assert.match(css, /\.draft-review-wrap\{max-width:100%;overflow-x:auto;/);
  assert.match(css, /\.draft-review-wrap:focus-visible\{outline:3px/);
  assert.doesNotMatch(jsx, /fetch\(.*(?:orders|settings\/apply|broker)/);
});

test("local draft reset cannot be confused with portfolio reset; controls retain touch targets", () => {
  assert.match(jsx, /<button type="button" onClick=\{resetRiskDraft\}>Reset local draft inputs only<\/button>/);
  assert.doesNotMatch(jsx, /<button[^>]*>Reset defaults<\/button>/);
  assert.match(css, /\.risk-draft-panel input,\.risk-draft-panel select\{[^}]*min-height:44px\}/);
  assert.match(css, /\.draft-actions button\{[^}]*min-height:44px/);
});

test("risk preview announces a short persistent status, not the entire comparison table", () => {
  // A live region surrounding an entire scrollable table can announce all rows
  // after each validation. Keep feedback present and separate from the data.
  assert.ok(jsx.includes('<p className="draft-feedback" role="status" aria-live="polite">'));
  assert.ok(jsx.includes('riskPreview ? <strong>Validated local preview — NOT APPLIED</strong>'));
  assert.ok(jsx.includes('Draft not validated or applied.'));
  assert.ok(jsx.includes('{riskPreview && <div className="draft-result">'));
  assert.ok(!jsx.includes('className="draft-result" role="status"'));
  assert.ok(jsx.includes('role="region" aria-label="Draft risk comparison table, horizontally scrollable"'));
});


test("P0 #42 paper execution gate is visible, persistent, and never mistaken for an attested halt", () => {
  // This must remain visible independently of the static reference GET result,
  // selected chart, local risk preview and unknown account snapshot.
  assert.ok(jsx.includes('<section className="release-gate" aria-labelledby="paper-release-title">'));
  assert.ok(jsx.includes('<h2 id="paper-release-title">Unattended paper execution BLOCKED — P0 #42</h2>'));
  assert.ok(jsx.includes('emergency-stop state remains UNKNOWN.'));
  assert.ok(jsx.includes('Reference GET checks and local risk previews do not authorize orders'));
  assert.ok(css.includes('.release-gate{display:flex;'));
  assert.ok(css.includes('@media(max-width:560px){.release-gate{'));
  assert.ok(!jsx.includes('Multi-session paper execution READY'));
});


test("paper-only and synthetic/user-supplied provenance badges remain visible at narrow widths", () => {
  // The earlier 850px media query hides .top-center. A later override must
  // explicitly restore the badges on tablet and 320px mobile layouts.
  assert.ok(jsx.includes('<span className="chip blue">PAPER ONLY</span>'));
  assert.ok(jsx.includes('showingLocal ? "USER-SUPPLIED DATA" : "SYNTHETIC DATA"'));
  const overrideStart = css.indexOf("/* Critical research provenance cannot disappear");
  assert.ok(overrideStart > css.indexOf(".top-center{display:none}"));
  const override = css.slice(overrideStart);
  assert.ok(override.includes("@media (max-width:850px)"));
  assert.ok(override.includes(".top-center { display:flex; flex-wrap:wrap; gap:8px }"));
  assert.ok(override.includes("@media (max-width:560px)"));
  assert.ok(override.includes(".top-center { flex:1 1 100% }"));
  assert.ok(override.includes(".top-center .chip { white-space:normal; overflow-wrap:anywhere }"));
});

test("a stale async local snapshot cannot reappear after clear, symbol change or a newer read", () => {
  // Source guardrails: these verify intent, not browser event ordering.
  assert.match(jsx, /const importRequestId = useRef\(0\)/);
  assert.match(jsx, /const requestId = \+\+importRequestId\.current;/);
  assert.match(jsx, /const snapshot = parseLocalDailySnapshot\(await file\.text\(\)\);\s+if \(requestId !== importRequestId\.current\) return;\s+setImported\(snapshot\)/);
  assert.match(jsx, /catch \(error\) \{\s+if \(requestId !== importRequestId\.current\) return;\s+setImported\(null\)/);
  assert.match(jsx, /function clearLocalImport\(\) \{\s+importRequestId\.current \+= 1;\s+setImported\(null\);/);
  assert.match(jsx, /onClick=\{clearLocalImport\}>Clear import/);
  assert.match(jsx, /onClick=\{\(\)=>\{clearLocalImport\(\);setSelected\(x\.symbol\);\}\} aria-pressed/);
});

test("a replacement snapshot clears previous bars before the read and permits cancellation/retry", () => {
  // Static contracts only; deferred File.text() races require browser testing.
  const importHandler = jsx.slice(jsx.indexOf("async function onSnapshotFile"), jsx.indexOf("useEffect(() => {"));
  const beforeAwait = importHandler.slice(0, importHandler.indexOf("await file.text()"));
  assert.match(beforeAwait, /const file = event\.currentTarget\.files\?\.\[0\];\s+if \(!file\) return;/);
  assert.match(beforeAwait, /event\.currentTarget\.value = "";/);
  assert.match(beforeAwait, /setImported\(null\);\s+setImportError\(""\);\s+setImportPending\(true\);/);
  assert.match(importHandler, /finally \{\s+if \(requestId === importRequestId\.current\) setImportPending\(false\);\s+\}/);
  const clearHandler = jsx.slice(jsx.indexOf("function clearLocalImport()"), jsx.indexOf("async function onSnapshotFile"));
  assert.match(clearHandler, /importRequestId\.current \+= 1;\s+setImportPending\(false\);\s+setImported\(null\);/);
  assert.ok(jsx.includes('onClick={clearLocalImport}>Cancel pending import</button>'));
  assert.ok(jsx.includes('className="import-progress" role="status">Reading local snapshot'));
  assert.match(css, /\.import-panel \.import-progress\{flex-basis:100%;/);
});
