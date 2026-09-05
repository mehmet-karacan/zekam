# WP-08 startup provenance review

Scope: the required-fragment increment within WP-08, not full SessionStart acceptance.
The user delegated further in-scope technical approval to independent agents after
approving the v3 control proposal. The independent startup reviewer approved this
increment subject to the following conditions, now exercised by independent tests.

- Keep the admitted operational schema unchanged. Do not create or activate config,
  work, learned state, or provider authority from startup context.
- Source slices retain exact project snapshot revisions and the real bounded source
  allowlist. Policy, work and run fragments retain their own truthful canonical IDs
  and revisions; never label them with a project Git revision.
- The trusted composite resolver verifies current row identity, digest, scope and
  bounded rendered bytes again while hydration holds the operational writer lock.
- Policy rendering is an explicit field allowlist, not the entire configuration.
  Required policy/work/run/source fragments may not disappear to fit a small budget.
- Existing same-work/run predecessor jobs, leases, pending delivery and unknown
  effects must prevent successful hydration until explicitly reconciled.
- Validate actual persisted lifecycle observations through the reviewed decoder;
  a caller-supplied internal event name is not proof of a SessionStart hook.
- No generic ranking-policy relaxation, live hook installation, provider call,
  PostgreSQL access or implicit schema/config admission belongs to this increment.

The read-only knowledge adapter increment is limited to existing offline checkpointed
files. Active journals/WAL or source drift are explicit unavailability, never silent
stale reads or automatic maintenance. A reader cannot rebuild or checkpoint an index.

Remaining WP-08 work includes complete home/config composition, predecessor checkpoint
and knowledge-note integration, bounded project/global retrieval, current installed
client contract acceptance, and end-to-end lifecycle evidence. Learned memory/skill
activation belongs to WP-09 and must not be fabricated in WP-08.

This document records a design review; it grants no runtime authorization.

## Subsequent integration evidence (macOS only)

The home/config gate, bounded note/retrieval composition and source-byte validation
were subsequently implemented. Independent environment integration passed 18 cases
and a 193-case combined regression: invalid home/config fails before spool/source
access, and configuration drift after an uncommitted manifest insertion rolls back
both manifest and receipt. One successful guarded hydration measured 0.387 seconds;
this is a sample, not a percentile or a scale acceptance claim.

A separate opt-in integration used actual local BAAI/bge-m3 vectors (1024 dimensions)
for two fragments of one allowed Akilli Kasa source file. After index creation,
provider and network access were prohibited. Startup, checkpoint and index reopen
in read-only mode passed with source/generation pins. This does not claim a dense
query occurred inside a hook, and does not replace the WP-07 RAG evaluation gates.

Evidence remains outside the source repository in
`/Users/mkaracan/zekam-wp08-v3-evidence.16wYdB/`. Neither this incremental evidence
nor a native client version probe closes WP-08 or the global task.

## Predecessor checkpoint metadata design

Independent verifier `wp06_quality_verifier` approved the following bounded design
under the user's standing in-scope review delegation. Implementation acceptance
requires separate independent tests; design approval is not a runtime permission.

- No schema change. Select a different session with exact project, realm, work,
  run, source snapshot, task, policy and plan identity. Do not disclose foreign
  realm/project/work history. Distinguish absent history from incompatible history.
- Deterministic latest selection uses creation time and checkpoint digest. Replay
  verifies the exact pinned immutable checkpoint, not whichever row is now latest.
- Verify canonical session ownership, checkpoint body/columns, durable event chain,
  covered event boundary, covered spool-prefix digest, historical context partition,
  fragment hashes/token accounting and canonical hydration receipt reconstruction.
- Read within a consistent, read-only operational snapshot. The startup composite
  resolver repeats verification while hydration holds the operational writer lock;
  current admission and pending/recovery checks remain mandatory.
- Do not read the predecessor's original spool, current source files or index merely
  to validate historical evidence. Never reactivate its old fragments, instructions,
  approval, lease or execution authority. Current sources are validated separately.
- Render metadata only: checkpoint/manifest identity, covered boundary and at most
  16 portable source references with an exact omitted count. Cap rendered metadata
  at 16 KiB; cap historical manifest verification at 1 MiB and event verification
  at 10,000 rows, failing closed rather than silently truncating integrity checks.
- Malformed compatible history must fail explicitly, not silently fall back to
  an older apparently healthy checkpoint.

Independent verification subsequently passed 72 checkpoint-specific cases and a
300-test combined regression. The actual local BGE test also passed with two
successive sessions: a read-only reopened index supplies current source citations,
while predecessor metadata retains its original immutable pin. Provider access is
blocked after indexing. See `checkpoint-stage-summary.md` in the external evidence
directory for exact test reports, source digests and remaining acceptance gaps.

## Bounded note extension

Independent review additionally approved explicit global knowledge context selection:
`additional_scope_refs` is either empty or exactly `("global-user",)`. Empty requests
retain their original serialized bytes and digest. This exception applies only to
KNOWLEDGE/global-user candidates; their proximity remains EXTERNAL, not project or
realm. Persisted context validation must retain the explicit opt-in.

The trusted note resolver requires the exact same realm, truthful owner scope,
materialized active manifest, physical file digest and materialization evidence.
Global notes must have no project ID or slug. Selection is bounded; no vault walk
or implicit inbox promotion is permitted. Generated notes retain provenance and
candidate/evidence-only labels. Existing knowledge state is not learned activation.

## Actual bounded-source recipe and production wiring

The coordinator's explicit source recipe was independently verified by 93 dedicated
tests and 228 overlapping source/startup/checkpoint/import-boundary tests. It binds
actual Git HEAD, selected source bytes and lengths, ancestor ignore files and local
`.git/info/exclude`, current task/policy/source ownership and the secret rule digest.
It reads only 1..8 explicitly selected tracked files. No whole-tree scan or old-state
conversion occurs. This recipe requires a physical contained `.git` directory;
unsupported worktree pointers are rejected, not resolved by guessing outside paths.
Snapshot apply is an explicit reviewed operation against existing local authority,
with source re-observation and rollback. Replaying a superseded tuple cannot return
an old snapshot as if current. See external `source-plan-stage-summary.md` for exact
evidence and limits; observations are not an atomic filesystem lease.

Independent reviewer `wp05_quality_verifier` approved the next existing-state startup
wiring design. It requires environment admission first, the exact already-admitted
source/session, a bound real source probe (no caller-supplied arbitrary callback),
same-buffer full-file verification before fragment slicing and repeated admission
inside the hydration writer window. Optional index input must be an exact read-only
SQLite index; unavailable retrieval must retain an open acceptance gate. A combined
environment/source report must digest its added source evidence, retaining the
original environment evidence separately. No bootstrap, schema change, provider call
or hook activation is implicit. This wiring is under independent implementation test;
structural decoder validation is not proof of an installed Mac client's lifecycle.

The bounded wiring subsequently passed 52 independent tests and the actual local
BGE two-session test. See external `composition-stage-summary.md` for exact counts,
source hashes and measured latency; startup samples currently take several seconds.
This acceptance does not include the following lifecycle integration increment.

## Frozen close integration review

An end-to-end test reproduced a real mismatch: after a valid startup context enters
closing state, reopening its frozen close request rerendered the context through the
startup-only open-session/no-pending guard. Independent review approved this narrow
separation, not a relaxation of startup admission:

- Fresh freeze still requires an open session, no pending effects and live context
  verification. No generic skip-resolver flag is exposed.
- Replay first compares the exact existing frozen semantic input. An existing load
  requires closing/closed state, exact request/job/outbox/input parity and the frozen
  checkpoint/event boundary before reading historical context evidence.
- Historical context uses the already-tested immutable manifest/receipt validator;
  it does not rerender prior fragments as current instructions or inherit authority.
- Current environment and real source verification still occur inside the close
  writer transaction. Compile effects, generated file bytes, delivery evidence and
  final close receipt checks remain mandatory.
- The SELECT-only `source_content_digest` observation now uses a query-only read
  snapshot. Its previous writer lock caused lock inversion when called by the
  required inside-writer source probe. Binding and exact snapshot checks remain.

The original close reproducer now passes and 235 existing close/control/checkpoint
tests passed after the read-lock correction. Independent review subsequently verified
157 distinct cases: 137 existing cases and 20 composed-flow cases covering corruption,
restart, partial publication and explicit recovery. The first combined run had 154
passes and one stale-collected test SQL typo; the corrected case and two subsequently
added cases passed in separate runs. This is not represented as one all-green run.
See external `frozen-close-independent-review.json` for exact reports and hashes.
The independent test author also completed a clean, frozen 20-case composed run
(`composed-close-independent-second.xml`, 555.26 seconds) without further edits.
The test-only local bookkeeping sink writes, fsyncs and rereads actual canonical
bytes; it is not external publication or a production dispatcher implementation.

The next integration subsequently used the existing production CLI runtime publisher
instead of that test-only sink. It verified real journal bytes and delivery receipts,
no-op repeated delivery and terminal close/replay. Independent review also reproduced
and fixed a publisher symlink redirection defect without changing its TSV or receipt
contract. See external `production-outbox-stage-summary.md`; this bounded evidence
does not include unrelated append paths, installed hooks or global task acceptance.

Five separately reviewed one-off native Codex probes remain failed, with no natural
SessionStart/SessionEnd receipts. Narrow sandbox changes progressed from the loader
to current-directory admission and then isolated client initialization; they did not
establish lifecycle acceptance. Source-project contents and user configuration remain
outside the write allowance. Native integration and hook latency remain open gates.
The fifth attempt timed out. Its original zero output counts were unavailable capture,
not observed empty output; the immutable result is retained with a separate correction.
Independent review found that hook children can start separate process groups. No next
native attempt is admitted until bounded cleanup covers that boundary independently.

The process-group supervisor then passed 99 pure tests and independent review. A sixth
immutable one-off bundle, independently approved only for exact `/bin/zsh` read/exec,
was run once and also failed by timeout with no natural receipts. Output was marked
unavailable rather than observed empty. No retry or wider sandbox permission followed;
installed-client lifecycle acceptance remains open.

A seventh exact one-off bundle added only the framework runtime executable observed in
the sixth denial. It passed in 1.01s with independently validated, same-session natural
SessionStart and SessionEnd receipts from the pinned Mac Codex binary and reviewed
ephemeral hook command. This accepts native event observation only. The parent did not
echo the hook's requested startup stop marker, so control/abort enforcement, installed
global activation, hydration, compaction, real-model execution and full lifecycle
acceptance remain explicitly unproven.

Post-proof source audit found a prior architectural assumption was wrong: Codex 0.151
`Stop` has turn scope and runs after ordinary turns, so it cannot be mapped to
authoritative `PRE_CLOSE`. `SessionEnd` is an advisory teardown hook and cannot create
`SESSION_CLOSED`; the terminal close receipt remains the authority. Existing tests that
used those structural mappings are not natural-close acceptance evidence. The v1/v2
close pipeline is valid as an explicit internal API, but native close wiring is blocked
pending removal of those mappings and a reviewed internal PRE_CLOSE boundary. This
finding reopens that portion of the earlier close integration acceptance.

## Existing-state command composition

The new `continuity local` commands connect already-admitted local state to drain,
hydration, checkpoint/resume and the dedicated freeze/compile/delivery/finalize pipeline.
They require an exact existing session, home and bounded source recipe. They do not
allocate a session, bootstrap a home, activate hooks or return native ACK. Historical
frozen diagnostics and advisory control drain remain separate from current-source
mutation admission. Doctor uses read-only observations and verifies frozen generated
bytes without repair. A substituted FIFO is opened nonblocking and rejected as a
non-regular projection, rather than hanging the diagnostic command.

The CLI builder verified 42 cases, including actual subprocess rejection of missing
pre-compaction events/unpersisted delta and fresh-process golden resume with real
Akilli Kasa source bytes. Common admission regression passed 73 cases after its exact
expected set was extended by only the five independently reviewed mutating leaves;
unknown neighboring commands retain fail-closed classification. The knowledge-plane
nonblocking-read regression passed 54 cases. Independent composed functional review
and the stricter existing-only runtime constructor are a separate pending increment.

The 4,113-pass/721-skip full-suite checkpoint predates these new commands, composition,
runtime admission and FIFO changes; it is not their acceptance evidence. Most skipped
tests are legacy PostgreSQL, and Windows/native opt-in cases are not certified by skips.
Installed client context injection, native checkpoint-aware responses, complete event
producers and the remaining close candidate contract still prevent WP-08 completion.

Independent review subsequently passed 40 exact-current composed cases, including
current/historical separation, checkpoint/restart/freeze/finalize, missing or deleted
runtime configuration without implicit initialization, database-path disappearance
without recreation, in-writer config drift rollback and the nonblocking FIFO case.
Production/test lint, formatting and typing passed. This accepts the bounded
existing-state command composition, not an installed client lifecycle or all WP-08.

The current two close projections satisfy the structured daylog/handoff contract but
do not prove distinct memory/decision/skill/failure candidate compilation. Independent
review approved an exact opt-in v2 recipe for new closes only. Existing v1 request
bytes, two projections and every recursive receipt digest remain permanently rendered
by v1. V2 accepts only explicit literal candidate claims whose source/evidence pairs
are subsets of the already admitted summary; empty categories produce deterministic
abstention artifacts instead of inferred skills or memories. Its reviewed recipe
digest is
`sha256:bbaab5423540620e4764e2c379d9cdd5ae919aa464fe46b9e5a1b375fe2558b3`.
The corrected engine passed 213 distinct independent tests after fixing a real
lone-surrogate typed-rejection defect and an initially omitted format check. It
deterministically renders six v2 projections and leaves all four candidate sets in
the inbox. The explicit lifecycle/CLI bridge remains under review and is not natural
Codex close evidence because of the separate Stop-mapping defect. The approval excludes
default activation, DDL, providers, promotion and WP-09 learned-state transitions.

The separate `freeze-v2` CLI then passed independent review: 63 CLI cases, 81
candidate/admission regressions and three additional descriptor/parser adversarials.
It requires a distinct bounded candidate file, preserves the v1 command, admits only
the exact new leaf and verifies a six-projection receipt across restart. This is
accepted as an explicit structural Zekam close surface only; it is not native close
evidence while Stop/SessionEnd mapping correction remains open.
