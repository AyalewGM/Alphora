import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Offline source contract; no browser, network, broker, or runtime side effects.
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
test("read-only terminal sets restrictive resource-loading and submission policy", () => {
  const tag = html.match(/<meta\s+http-equiv="Content-Security-Policy"[^>]*>/i)?.[0];
  assert.ok(tag, "CSP meta tag must be present in the static entrypoint");
  for (const directive of [
    "default-src 'self'", "script-src 'self'", "connect-src 'self'",
    "object-src 'none'", "base-uri 'self'", "form-action 'none'"
  ]) {
    assert.ok(tag.includes(directive), `Missing CSP directive: ${directive}`);
  }
  assert.doesNotMatch(tag, /script-src[^;]*'unsafe-inline'/);
  assert.doesNotMatch(tag, /connect-src[^;]*https?:\/\//);
});
