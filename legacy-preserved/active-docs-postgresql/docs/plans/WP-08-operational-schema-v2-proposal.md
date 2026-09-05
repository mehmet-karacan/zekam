# WP-08 operational schema v2 proposal

Status: approval-required; not activated.
Authority: AKTIF_GOREV.md Sections 11.5, 16, 22.2 and WP-08.
Revision: 2; supersedes the earlier proposal containing a second outbox family.
This document is a proposal, not approval evidence or an implementation receipt.

## Exact scope

Keep the existing single local `state/operational.db` authority. Add a forward,
transactional schema v2 migration only from the exact verified fresh local v1
schema. Unknown, legacy or checksum-drifted databases remain rejected. No separate
continuity database is introduced. No PostgreSQL connection, import, export, dump,
ETL or historical data reconstruction is permitted.

Add these local table families, reusing existing project, work, run, session,
session_event, local_effect_claim and local_effect_receipt identities:

- `continuity_session_binding`: exact session/project/work/run/client/device/realm,
  source revision and task/plan digest binding; explicit read-only scope when no
  work/run is bound, with no implied mutation authority.
- `session_event_detail`: typed lifecycle kind, sequence, previous digest,
  content-free bounded metadata and idempotency key for each existing session event.
- `continuity_effect_binding`: exact session to existing effect-claim linkage.
- `continuity_outbox_binding`: session/job/purpose/input-digest linkage to the
  existing `local_outbox`. Reuse `local_outbox_delivery`, `local_outbox_receipt`
  and `local_recovery_case`; do not create a second durable delivery family.
- `continuity_checkpoint`: immutable covered event sequence, source/task/context
  digests and durable pre-compaction ACK evidence.
- `context_manifest` and `hydration_receipt`: exact bounded model-visible fragments,
  token budget, source refs and checkpoint binding.
- `continuity_close_request` and `close_receipt`: frozen bounded close input,
  pending/recovery state and terminal daylog/handoff evidence.

## Required invariants

- Existing local user records are preserved; no reset, cleanup or overwrite.
- Lifecycle observations never carry raw prompts, responses, transcripts or secrets.
- Required spool/outbox failure is fail-loud; no terminal receipt means pending.
- Pending effect, undelivered outbox or unpersisted session delta blocks compaction ACK.
- Session start uses bounded context; it does not preload the full vault.
- Generated daylog/handoff remains a candidate/projection, not an active fact.
- Generated Markdown has immutable source refs/digests and stable replay bytes.
- Half-written projections are recoverable by idempotent replay without overwriting
  user-authored files.
- Concurrent event/checkpoint/close operations cannot mix sequence or owner scope.

## Upgrade and restore contract

- Preserve the original v1 SQL bytes, migration name/checksum and schema fingerprint
  as immutable versioned constants. Do not edit v1 SQL and relabel it as v2.
- Keep status read-only and version-aware: distinguish exact supported v1, current
  v2 and rejected unknown/drifted states. Reading status never performs an upgrade.
- Fresh-v2 bootstrap and exact-v1-to-v2 upgrade use the same ordered migrations;
  resulting schema fingerprints and migration ledgers must match. Existing v1
  records and their content digests remain unchanged.
- Validate the complete v1 schema, both ledgers, integrity and foreign keys before
  upgrade. Apply v2 DDL, its ledger entries and current-version metadata in one
  transaction; interruption leaves either verified v1 or verified v2, never a
  partly admitted schema. Competing upgrades serialize and revalidate.
- Upgrade requires quiescent writers. Live leases, claimed deliveries, unresolved
  effect outcomes or recovery cases prevent admission; do not kill workers or
  manufacture receipts to make the upgrade pass. Unknown old rows are not
  reconstructed from Markdown, RAG or PostgreSQL.
- A v1 backup remains immutable. Verify it against the exact v1 manifest, restore
  only to a new empty target, verify pre-upgrade logical parity, and then use the
  same explicit v1-to-v2 path. Verify parity of original tables separately from
  the new schema metadata. Never compare a v1 whole-DB digest with a v2 digest.
- Restored homes are offline until restore admission succeeds. This bounded path
  admits only snapshots with no live or unresolved execution/delivery authority;
  snapshots containing such state remain recovery-required and are not started.
  Startup uses a fresh process identity and never accepts copied lease/owner
  tokens as authority. Migration and restore tests must attempt replay of old
  claims and confirm rejection. A broader active-snapshot recovery design is not
  silently authorized by this proposal.
- Existing destination paths, corrupt snapshots, ledger drift and unsupported
  versions fail without overwrite. No destructive downgrade is introduced.

## Queue reuse and scope admission

- A compile request binds to a real local job, its session and one typed purpose.
  Insert request and binding atomically. Keep the existing payload-bound replay,
  owner/PID/token/fence, terminal receipt and recovery rules.
- Current `claim_outbox` selects every pending kind. Before lifecycle producers
  are enabled, introduce explicit supported-kind selection and typed dispatch.
  A consumer must not claim an unsupported kind; unknown kinds remain visible
  and pending, not silently delivered or dropped. Existing runtime consumers and
  all their call sites require regression coverage.
- In a single transaction, validate session.project = work.project,
  session.work = run.work, job/work/run ownership and effect.job/lease/fence.
  Validate client/device/realm, exact source revision and task/plan digests too;
  separate foreign keys alone are not sufficient proof.
- Binding cannot create work, lease, approval or mutation authority. A read-only
  session cannot acquire another work item's effect/receipt. Cross-session,
  cross-project, cross-realm and stale-source references are rejected.
- Pre-compaction checks the existing delivery and recovery state through exact
  bindings. A worker spawn is not delivery; timeout/unknown is not success.

## Event, checkpoint and close contract

- Enforce unique `(session_id, sequence)` and `(session_id, idempotency_key)`.
  Sequences are contiguous; each event binds the prior event digest. Exact replay
  returns the same identity/bytes; the same key with changed payload is rejected.
- Checkpoint creation atomically captures the last covered event sequence/digest
  and checks pending effects, undelivered outbox, unresolved recovery and durable
  spool cursor parity. Missing/unpersisted deltas block ACK. Future deltas are
  never implicitly covered by an older checkpoint.
- Close freezes an exact event boundary and input digest transactionally. Exact
  replay is accepted; new ordinary events after freeze are rejected visibly.
  Worker completion/recovery observations use the close request's separate
  control path, not backdated session deltas. Concurrent append/close must produce
  one deterministic winner without dropping accepted events.
- Validate source identities before compilation and again before final publish.
  Source drift keeps close pending/recovery-required; it cannot produce a success
  receipt from stale inputs. Close receipt references the exact checkpoint,
  manifest, generated files and their digests.
- Daylog contains work performed, work/run scope, decisions, failures, evidence,
  remaining work, next safe step and source/receipt refs (Section 16.6).
  Daylog/handoff and memory/decision/skill/failure outputs remain candidates;
  no automatic fact or skill promotion is authorized.
- Publish generated bytes only through the existing safe knowledge-file boundary.
  A user-authored file or changed destination is a conflict, not permission to
  overwrite. Partial publish is recovered from immutable inputs and recorded
  file digests. Verify required files before the terminal close receipt; a crash
  before/after that receipt cannot create contradictory completion or duplicates.
- Gap/recovery doctor is read-only by default. It reports missing sequences,
  spool cursor gaps, undelivered jobs, missing receipts and projection conflicts.
  Bounded repair may replay exact durable spool events or rebuild generated
  projections; it must never invent missing events, facts or authority. Missing
  evidence and ambiguous effect outcomes stay recovery-required.

## Validation and activation boundary

Implement and test the migration and adapters in disposable local test homes first.
Force duplicate, null/wrong type, empty/oversized values, corrupt metadata,
timeout/provider failure, partial delivery, process death, restart/recovery and
concurrent sequence scenarios. Provider failure cannot disable required durable
event capture; no provider call occurs inside the hook.

Required evidence beyond existing component tests:

- Fresh-v2 versus upgraded-v1 fingerprint parity, preserved v1 rows/checksums,
  corrupt/unknown schema rejection, upgrade crash rollback and concurrent upgrade.
- v1/v2 backup verification, empty-target restore, pre/post-upgrade parity,
  active-snapshot admission rejection and stale owner/claim replay rejection.
- Mixed consumer kinds, duplicate/different-payload requests, wrong consumer,
  expired claim, forged receipt, timeout, process-kill and recovery replay.
- Cross-project/work/run/session/realm ownership attacks and source/plan drift.
- Event gap/duplicate/concurrent append, close/append race and checkpoint capture
  race; no ACK with pending effect/outbox/unpersisted delta.
- On-disk SQLite integration: hydration -> committed events -> durable checkpoint
  -> process restart -> golden resume -> close -> terminal receipt, reopening the
  actual disposable DB rather than substituting an in-memory repository.
- Missing required hook fail-loud, deterministic budgets without full-vault
  preload, immutable source refs, interrupted projection publish, user-file
  collision and receipt-before/after-crash recovery.

Re-run existing client adapter, golden resume, context budget, spool, local-runtime
and security suites. Passing their fake-client/in-memory tests is component
evidence only; it does not satisfy the integrated SQLite continuity gate.

This approval does not authorize installing or activating hooks in Codex, Claude
or OpenCode user settings. Hook activation requires a separate exact reviewed plan.
It does not authorize live model benchmarks, remote disclosure, commit push,
retention changes or destructive operations. Windows live E2E remains deferred
under K-013 and is not considered passed.
