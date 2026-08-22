# Zekam Test Stratejisi

## Test piramidi ve kanıt

### Unit
Pure domain validation, canonical digest, classifiers, RRF, locks, policies, memory lifecycle.

### Property/fuzz
JSON/schema, path normalization, DAG acyclicity, lock conflict, canonical hashing, authorization
scope, archive entries, parsers.

### Integration
PostgreSQL transaction/RLS/migration, object storage, queue/lease/fence, claim/receipt,
provider adapters with controlled fake, Mem0 adapter, pgvector/FTS.

### Concurrency
Multi-worker claim, stale fence, lock ordering, outbox, idempotent enqueue, completion race,
memory promotion race.

### Security
Prompt injection, secret canary, SSRF, traversal/symlink/archive, cross-project RLS, auth
replay/expiry/scope, malicious child/MCP/provider output.

### Evaluation
Model benchmark, retrieval golden set, reranker, ASR WER/CER, guardrail FP/FN, VL grounding,
memory context effectiveness.

### E2E
Natural language → Work → subagent research → continuity;
research → plan → worktree patch → test → verifier → receipt;
model quota fallback;
document/repository ingestion → citation;
crash → recovery;
backup → restore.

## Deterministik fixture

- Secret içermeyen sentetik/sanitized.
- Exact source/artifact digest.
- Versioned expected schema/evidence.
- Local-only data policy where required.
- Stable clock/UUID injection.

## PostgreSQL acceptance

Gerçek PostgreSQL 18+pgvector container:
- migration upgrade/downgrade/forward-only notes,
- constraints/RLS,
- queue races,
- vector/FTS,
- backup/restore sample.

SQLite production behavior taklidi değildir; yalnız sınırlı local test adapter varsa unsupported
capability açıkça blocked.

## Negative tests

Her güvenlik ve invariant için en az bir negative test:
- no subagent for agentic
- two builders same path
- result without receipt
- same builder/verifier
- stale source/plan
- unknown model capability
- guessed quota
- memory as Work state
- raw endpoint/secret active inventory
- dashboard mutation bypass
- commit non-ASCII.

## Flaky test

Retry ile gizleme yok. Flaky test quarantine issue ve root cause ister. CI pass için test
silme/skip yalnız explicit policy/evidence.

## Coverage

Yüzde tek başına kalite değildir. Kritik domain/harness/security branches için branch coverage
ve mutation testing hedefi; adapter integration/E2E evidence ayrıca.

## Reproducibility

Her test/eval raporu:
- release/commit
- environment/image digest
- policy/config
- source/fixture
- model/inventory/profile
- seed/as_of
- command
- result artifact digest.
