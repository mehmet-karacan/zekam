# `zekam doctor` Runbook

## Amaç

Kurulum, dependency, state integrity ve external capability'yi mutation yapmadan raporlar.

## Kontroller

### Core
- release/version/checksum
- Python/runtime
- config parse
- ZEKAM_HOME permission/layout
- Git client.

### PostgreSQL
- connection
- PostgreSQL 18 compatibility
- pgvector version
- migration head/drift
- required extensions
- RLS/constraints
- queue/ledger integrity.

### Object storage
- endpoint/ref
- bucket/access sentetik
- checksum roundtrip (isteğe bağlı local safe)
- orphan/missing artifact summary.

### Queue/runtime
- scheduler/worker heartbeats
- stale leases/locks
- claim/no receipt
- outbox backlog
- recovery-required.

### Clients
- Codex/Claude/OpenCode executable/version
- capability declaration
- authenticated state only as boolean/reference
- quota observation availability
- no credential echo.

### Models
- inventory 20
- technical profile 19 mismatch visible
- latest health/benchmark age
- quarantine
- endpoint refs resolved availability (value echo yok).

### Knowledge
- active embedding profile BGE-M3 1024
- index current/stale/corrupt
- FTS/vector sanity
- parser/OCR capability.

### Memory
- native engine
- Mem0 adapter optional health
- sync backlog
- hygiene due.

### Security
- secret store access metadata
- insecure file permissions
- debug/CORS/network flags
- source roots/write risk
- backup encryption policy.

## Çıktı

```text
healthy
degraded
blocked
recovery-required
```

Her finding:
- code,
- severity,
- evidence digest/ref,
- safe next action,
- authority requirement.

Doctor secret veya auto-migration yapmaz. Repair ayrı dry-run/exact apply operation'dır.
