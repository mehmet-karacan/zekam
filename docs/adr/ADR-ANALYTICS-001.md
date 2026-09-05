# ADR-ANALYTICS-001: Rebuildable local analytics projection

- Status: Accepted for macOS ARM64; Windows x64 deferred
- Date: 2026-09-02
- Authority: `AKTIF_GOREV.md` technology gate 8.3

## Context

Analytics is a derived projection over immutable benchmark and telemetry
artifacts. It must not become operational authority or accept operational
mutations. The store must be removable and rebuildable from raw artifacts.

DuckDB 1.5.5 currently publishes macOS ARM64 and Windows x64 Python wheels.
Official concurrency documentation supports read/write concurrency inside one
process, while stable multi-process native-file writing is not the default
model. This matches Zekam's required single analytics writer or batch importer.

## Measured evidence

With explicit user approval, DuckDB 1.5.5 was installed only in an isolated
temporary Mac environment. A 100,000-row immutable JSONL artifact was imported;
four read-only child processes completed concurrently, a second writer process
was rejected by the native file lock, and deleting/rebuilding the derived DB
reproduced the exact logical projection digest. No network attempt occurred and
no operational mutation API was exposed.

A half-truncated copy was still readable by DuckDB itself. The required
pre-open projection manifest detected the digest mismatch, so the corruption
gate passes through Zekam's manifest boundary, not through an unsupported claim
that the engine rejects every truncation.

## Decision

Use DuckDB only behind an analytics projection port with:

- one writer process and explicit batch import receipts;
- immutable raw benchmark/telemetry artifacts as source authority;
- no operational write API;
- deterministic rebuild and parity checks;
- no network-loaded or unsigned extension at runtime;
- separate platform/package evidence before activation.

## Gate

The Mac-first implementation may use DuckDB behind the projection port. Windows
x64 acceptance remains deferred until the same clean-install, import,
concurrent-read, writer-exclusion, manifest-corruption and delete/rebuild probes
pass there. No global cross-platform claim follows from this decision.
