# WP-08 post-close control evidence proposal

Status: approval-required; NOT IMPLEMENTED or activated.
Authority: AKTIF_GOREV.md Sections 16, 22.2 and WP-08.
This proposal does not supersede or change the approved v2 proposal bytes.

## Observed integration blocker

The approved v2 ordinary session event chain freezes at the close request's exact
checkpoint boundary. That is necessary: accepted work events cannot be backdated
into an already compiled and closed session.

The existing reviewed Codex/Claude adapters also emit advisory `SessionEnd`
(`post_close`). Independent reproduction with both real parsers, disposable local
homes and the read-only Akilli Kasa source showed:

- Before SessionEnd: terminal close receipt exists; doctor is healthy; ordinary
  events / persisted spool entries / actual spool entries = 2 / 2 / 2.
- After SessionEnd: drain rejects `Continuity session delta frozen`; the counts
  become 2 / 2 / 3 and doctor reports `unpersisted-spool-delta`.
- Retrying preserves every frozen row and the same close receipt but does not
  consume the advisory observation. A later Stop remains behind that observation.

This is safe rejection, not completed lifecycle integration. The generic spool
ACK is not a shortcut: its existing canonical continuity binding requires real
event/effect/receipt evidence. Manufacturing that evidence or reopening the
ordinary event chain would violate the approved contracts. A spool-only warning
can preserve observations, but cannot satisfy the integrated durable-control gate.

## Exact proposed change

Add one immutable `continuity_control_event` family in a forward operational v3
migration. Do not add another queue, outbox, lease, effect or recovery authority.

Each record binds:

- canonical session and binding digest;
- existing frozen close request; terminal close receipt is joined on read, never
  copied into and later patched onto an immutable pending observation;
- exact client/device/external-session identity;
- original durable spool entry digest, observation digest and delivery identity;
- one closed allowlist disposition: `advisory-post-close` or
  `rejected-after-freeze`;
- canonical bounded body, content digest, stable idempotency key and timestamp.

The key and source spool identity are unique per canonical session. Update/delete
are forbidden. Exact replay returns the same record; changed payload is rejected.
No raw prompt, response, transcript, tool payload or secret content is copied.
No record grants authority, renews a lease, changes work/run status or creates a
successful close receipt.

Processing rules:

1. Ordinary pre-freeze events still use the existing contiguous session chain.
2. After freeze, a reviewed advisory post-close event is recorded only through
   this control path. It does not extend checkpoint coverage or claim completion.
   A pending close remains pending until the existing worker/finalizer succeeds.
3. A genuinely new ordinary event after freeze is recorded as rejected and its
   caller receives an explicit failure. It is not silently dropped or admitted.
   Disposition is derived from the reviewed typed event and the immutable frozen
   spool boundary, never from a caller-selected disposition or client timestamp.
   Exact replay of a previously accepted ordinary delivery stays ordinary replay;
   it must not become a new rejected control observation after close.
4. Doctor distinguishes accounted-for advisory/rejected observations from actual
   missing data. Rejected ordinary observations remain visible attention items.
5. Keep the current generic effect-backed ACK contract unchanged. The local
   continuity progress is only a derived, contiguous processed-prefix of exact
   ordinary/control DB evidence. It is separate from the generic spool ACK/drain
   cursor, never writes a control receipt into that cursor, and never skips gaps.
   It must not fabricate a generic ACK or claim a worker spawn as delivery.
6. Source/config drift still blocks compilation and context reuse. Recording a
   historical advisory observation never revalidates or reactivates stale work.

## Versioning and preservation

Freeze the verified v2 SQL and fingerprint before adding v3. Preserve both v1
ledgers/fingerprints and every v2 row. Fresh-v3 bootstrap and exact-v2 upgrade use
the same ordered migration. Unknown, legacy and checksum-drifted DBs are rejected.
Upgrade is explicit and quiescent; no automatic upgrade, user-home overwrite,
worker killing or destructive downgrade. Test rollback, concurrent upgrade and
backup/restore parity on disposable homes first.

Verified v2 schema fingerprint at proposal preparation:
`sha256:812d64b984d774154a710b6bead73f065004bccb4a5d633b9aa4c64a42d5914d`.

## Required acceptance evidence

- Both reviewed command-hook parsers: complete close, then SessionEnd, restart,
  repeat drain, unchanged ordinary checkpoint/close bytes and accounted advisory.
- SessionEnd before worker completion cannot make a pending close complete.
- Pending observation -> worker finalize -> replay preserves exactly the same
  control record bytes; current terminal status is obtained by joining the close
  request's receipt on read, never updating the observation.
- Replaying an already accepted ordinary delivery after close is not reclassified.
- New Stop after freeze remains visibly rejected and never changes coverage.
- Missing source event, forged scope, null/wrong type, duplicate/drift, unknown
  disposition, cross-session/project/realm, concurrent drain and process death.
- A control record without the exact original durable spool evidence cannot
  advance a cursor; evidence mismatch remains recovery-required.
- No provider or PostgreSQL access, no new runtime mutation authority, no second
  outbox, and all approved-v2 regression gates remain in force.

## Exclusions and remaining work

This approval would authorize only implementation/testing of the above bounded
control path and migration. It would NOT authorize installation/activation of
client hooks, changing user settings, live model calls or benchmarks, PostgreSQL
access/import, push, destructive actions, or Windows acceptance.

WP-08 also still needs production startup composition (current policy and bounded
decision/failure/skill/retrieval fragments), current installed-client contract
review, and a separately approved exact hook activation plan. OpenCode idle/status
events must not be guessed to mean pre-close. WP-09 and later remain unaccepted;
this proposal is not a declaration that the whole task or WP-08 is complete.
