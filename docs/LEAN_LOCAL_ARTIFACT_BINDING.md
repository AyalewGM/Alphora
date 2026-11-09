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
- **Multi-file mutation guard**: capture directory inode and
  mtime/ctime before and after hashing all eight files. Reject
  `LOCAL_ARTIFACT_DIRECTORY_CHANGED` if entries or metadata changed during
  the pass, even when individual file hashes match. This conservatively
  blocks concurrent file-set changes (including unrelated entries).
  It is **not** a transactional snapshot, file lock, source authentication,
  or a guarantee that files will remain unchanged after the read.
- **No hidden fallback**: unsupported operating systems/filesystems,
  missing files, unsafe roots, detected concurrent edits, invalid metadata,
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

## Repeated three-leg suite binding (offline-only extension)

`lean_suite_binding.bind_repeated_local_lean_suites(...)` composes the
existing six-receipt metadata check with all **48** local file hash
verifications (two attempts × three legs × eight artifacts). Supply
`first_attempt` and `second_attempt` tuples of three matching receipt
records each, and `roots` and `filenames` as two corresponding
three-element tuples, positionally aligned with each attempt's receipts.
Each root must be a distinct trusted absolute directory *inode*, not
just a different path; reused directories, final-component symlinks,
invalid modes, mismatched outputs, bad or missing bytes and bad shapes
fail closed. Each of the six root directory identities is now rechecked
immediately before **and after** its eight-file binding, rejecting a
byte-identical directory replaced after the initial distinct-root
preflight (`SUITE_ARTIFACT_DIRECTORY_CHANGED`). This is a bounded
rename-race detection guard, **not** a filesystem snapshot or an
adversarial race-proof guarantee. Returned hashes are sorted by benchmark
mode, independent
of receipt input order and any absolute path spelling; they reveal
neither source file bytes nor paths.

**Critical limitation:** two directory copies can contain identical
fabricated artifacts; metadata or file hashes cannot establish that six
independent LEAN processes ran. This also cannot authenticate licensed
sources, prevent all path/rename races, verify parent-directory safety,
prove reproducible performance or reconcile engine numerical outputs.
Independent human QA must examine actual engine logs and execution
provenance in addition to these byte-level guards. Never commit real
licensed input data or credentials to GitHub.
