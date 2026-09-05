# WP-04 macOS acceptance summary

Status: **macos-accepted / windows-deferred**. Independent verifier: **PASS**.

The fresh SQLite operational authority provides a durable local job queue,
append-only transactional outbox, resource locks, leases, process-incarnation
tokens, monotonic fencing, heartbeat expiry, claim-before-effect records,
terminal receipts, explicit immutable unknown-effect resolutions, timeout
reconciliation and atomic scheduler slots. Terminal/recovery evidence and
recovery-case transitions are enforced by database constraints and triggers,
not only by adapter code.

The outbox pending bound is persisted as immutable operational configuration.
Every producer, including terminal, recovery and quarantine paths, rolls back
atomically when the bound is full. Outbox startup recovery is independently
composed so the consumer can drain a full queue before job recovery retries;
no hidden secondary backlog bypasses the bound.

Real CLI worker and outbox processes wrote fsync-backed external journals and
were killed with `SIGKILL` before receipt persistence. Restart retained the
claim, surfaced `recovery-required`, and left each external call count at one.
Short writes were forced and accepted only after every byte was written.
Wrong-type process probes, raw terminal-without-evidence writes, audit mutation,
pre-resolved cases and case/outcome mismatches were rejected before mutation.

The final focused set passed 128 tests; security/authority gates passed 71.
The independent verifier separately passed 78 tests and both real CLI SIGKILL
repros. The full suite produced 2624 passes and 712 intentional PostgreSQL
skips. Its five remaining failures are the pre-existing machine-contract drift:
three pinned client binary digests, Claude 2.1.252 versus the 2.1.224 pin, and a
Windows `zekam.exe` path assumption on macOS. No task regression remains.

No PostgreSQL connection, data access, import, export, migration, ETL or Docker
dependency was introduced. Windows stress remains deferred under K-013. WP-04
is accepted; the global task is not complete and the next dependency is WP-05.

## Current-source revalidation — 5 September 2026

The frozen current-source full suite passed 6,957 tests with 16 explicit skips,
zero failures and zero errors. The earlier five environment-drift failures are
retained above as historical context and are superseded by the bound JUnit
evidence in `test-results/full-suite.json`. Independent post-implementation
verification is recorded in `verifier-report.json`.
