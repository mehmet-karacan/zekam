# ADR-LOCAL-STORE-001: Embedded operational store selection

- Status: Accepted for macOS ARM64; Windows x64 deferred
- Date: 2026-09-02
- Authority: `AKTIF_GOREV.md` WP-01 and technology gate 8.1

## Context

Zekam needs a fresh, local operational authority that requires no container runtime, DB
server, credential, network filesystem, or legacy server-database data. The shortlist is
CPython SQLite and Turso Database through `pyturso`. Documentation and wheel
metadata are discovery evidence only; they are not load, durability, recovery,
or cross-platform execution evidence.

## Measured evidence

The macOS ARM64 CPython SQLite spike used Python 3.12.14 and SQLite 3.53.4.
SQLite 3.53.4 is beyond the upstream 3.51.3 WAL-reset fix boundary. The spike
executed 10,000 project inserts, 10,000 work inserts, 100,000 append-only
events, and another 10,000 events through four producers and one serialized
writer. It also exercised concurrent reads, foreign-key/unique/check rejection,
idempotent replay and payload drift, an uncommitted writer process kill,
an interrupted snapshot process, disk-full rollback, a read-only directory,
unexpected-trigger schema drift, file truncation detection, and SQLite
backup/restore logical digest parity.
All local probes passed and the network deny monitor observed zero attempts.

Canonical result:
`artifacts/acceptance/wp-01/wp01-d95cdac/metrics/sqlite-operational-macos-arm64.json`.

An earlier implementation assumption—copying only the main file while WAL was
active—failed to carry the schema. The spike therefore uses SQLite's backup API
for consistent copies. A future filesystem snapshot must include DB/WAL/SHM as
one unit or use the backup API.

With explicit user approval, `pyturso` 0.7.2 was installed only in an isolated
temporary environment and exercised against the same workload. It passed the
Mac load, constraint, idempotency, serialized-producer/concurrent-reader,
backup/restore, truncation, schema-drift, read-only, crash-integrity and offline
probes. Its embedded SQLite is 3.50.4, below the 3.50.7 backport boundary for
the WAL-reset fix, so its safe measured profile is a single-writer coordinator.
The runtime also retained a process-lifetime lock for a database path after
connections closed; the crash probe therefore used a separately copied,
previously unopened database as the child process target. This behavior is a
real operational constraint, not a hidden test relaxation.

The current `pyturso` release still has no Windows wheel. A real clean Windows
x64 source build and runtime test remains mandatory; classifiers are not
accepted as execution evidence.

## Decision

For the user-approved Mac-first phase, select CPython SQLite 3.53.4 as the
provisional operational engine. It has the smaller dependency surface, the
WAL-reset fix, the more mature backup/recovery path and no process-lifetime
path-lock behavior observed in pyturso. This is not Windows acceptance: the
global selection contract remains fail-closed until Windows x64 evidence exists.

## Consequences

- `pyproject.toml` remains unchanged.
- The legacy server database is not queried and is not a candidate data source.
- No Turso remote/sync mode is allowed in this bake-off.
- WP-02 may bind CPython SQLite on macOS under the explicit deferred-platform
  decision.
- If the runtime SQLite version lacks the WAL-reset fix, multi-connection WAL
  is disabled and a single-writer profile is mandatory.
