# WP-03 macOS acceptance summary

Status: **macos-accepted / windows-deferred**.

Operational Store schema v1 is a fresh SQLite authority for project, alias,
source snapshot, work revision/event, run, step, checkpoint, session, model
state, config/task digest and receipt records. Unit-of-work boundaries use
explicit transactions, rollback by default, schema fingerprint/integrity
verification and online backup parity. Legacy/unknown SQLite schemas are
rejected before mutation; no PostgreSQL data is read or transferred.

Fresh CLI project/work behavior, restart, duplicate, type/null/empty/digest
drift, rollback, process-kill, recovery and concurrency cases passed on macOS.
An AST security gate proves that `src/zekam/application/**` imports neither the
PostgreSQL nor SQLite concrete adapter packages. Historical PostgreSQL-backed
workflows now reach their adapter only through a fail-closed provider installed
at the outer composition root; the new local operational path does not use it.

The focused acceptance set passed 100 tests. The full suite produced 2544
passes and 712 intentional PostgreSQL skips. Its remaining five failures are
pre-existing machine-contract drift: three pinned client binary digests, one
Claude version pin (installed 2.1.252 vs expected 2.1.224), and one Windows
`zekam.exe` path assumption on macOS. Those security expectations were not
weakened. Windows acceptance remains deferred under K-013.

WP-03 is accepted. The next dependency is WP-04; no global task completion is
claimed.

## Current-source revalidation — 5 September 2026

The frozen current-source full suite passed 6,957 tests with 16 explicit skips,
zero failures and zero errors. The earlier five environment-drift failures are
retained above as historical context and are superseded by the bound JUnit
evidence in `test-results/full-suite.json`. Independent post-implementation
verification is recorded in `verifier-report.json`.
