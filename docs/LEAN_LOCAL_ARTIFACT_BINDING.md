# Locally binding LEAN research receipts to artifact bytes

This is an **optional, offline byte-integrity stage** for issue #20,
stacked on the existing metadata-only LEAN receipts in PR #26.

The PR #3 LEAN research sample is merged but is **not** evidence of an
executed LEAN backtest. Existing `lean_evidence.py` checks only the
*shape and consistency* of eight caller-supplied SHA-256 strings. The new
`lean_local_binding.bind_local_lean_artifacts(...)` checks that eight
individually named **local files** actually hash to the receipt's claimed
SHA-256 values. It never launches LEAN, downloads data, makes trades,
requests Alpaca credentials, or writes to a broker or the filesystem.

## Operator contract (without storing source files in GitHub)

An independent data operator places authorized input/output artifacts in
a **trusted absolute directory outside the repository**, pins each
independently, and supplies a validated `LeanBacktestReceipt` plus
a flat filename for each of these fields:

`algorithm_sha256`, `data_sha256`, `calendar_sha256`,
`corporate_actions_sha256`, `license_receipt_sha256`,
`settings_sha256`, `input_manifest_sha256`, `output_sha256`.

Pass `root=<trusted absolute directory>` and
`filenames={<receipt field>: <flat basename>, ...>`; the utility reads
files in 1 MiB chunks and checks bytes against the eight pinned receipt
hashes. It never logs, stores or returns file contents or absolute paths.
The returned local fingerprint includes artifact **names**, hashes and
the metadata receipt fingerprint. These fields are not signatures.

For confidentiality, input files, licensed market data, local directory
paths, license documents and any broker/API secrets must never be
committed to GitHub, placed in PR comments or sent to ChatGPT. Use only
fabricated, synthetic data for repository tests.

## Boundaries and fail-closed behavior

- **No path traversal or symlinks**: file selectors must be distinct,
  ASCII flat basenames, and each file is opened under a single local
  directory descriptor with no-follow flags; hardlinks, directories,
  pipes, zero-length and oversized files are rejected.
- **Bounded streaming**: individual artifacts are capped at 4 GiB by
  default, and can be restricted further. Data exceeding this bound
  require a separately reviewed chunked manifest protocol, not silent
  truncation or an increased unbounded limit.
- **No hidden fallback**: unsupported operating systems/filesystems,
  missing files, unsafe roots, concurrent edits, invalid metadata,
  mismatched bytes and anything other than `backtest` mode are rejected.
- **Trusted local root is still required**: ancestor directory symlinks,
  hard-to-detect filesystem races, re-creation of identical fabricated
  data, stolen evidence, and malicious operator behavior are not proven
  safe. A matching hash verifies **bytes only**, not vendor authenticity,
  point-in-time completeness, permission to use data, or the correctness
  of LEAN outputs.
- No actual backtest, benchmark, portfolio accounting or performance
  result is produced by this module.

## Next evidence gate

Independent data/financial QA must select and license a feed, independently
verify exchange session calendars and corporate actions (including
delisted/symbol-changed assets), and perform two complete isolated LEAN
backtest attempts for strategy, fully invested SPY total-return, and
10%-SPY-and-cash modes. Bind every actual artifact, retain independently
protected runtime logs and exit codes, and reconcile dated results, cost
assumptions and allocation limits against the reference simulator.
The per-file integrity bindings are not a substitute for those steps.

PRs #17 (offline replay), #19 (loader guards), #21 (manifest) and #26
(receipt metadata) remain separate review dependencies. Do not merge
without independent qualified human financial and QA acceptance and
current-head checks. No broker connections, paper orders, deployed
services, real-money or automated merge.
