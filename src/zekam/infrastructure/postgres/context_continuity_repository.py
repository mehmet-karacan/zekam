"""PostgreSQL context continuity append-only repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextManifest,
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
    JournalEntry,
)
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragment,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation
from zekam.domain.identifiers import new_uuid7


@dataclass(frozen=True, slots=True)
class ContextContinuityRepository:
    connection: Any
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID

    def store_manifest(self, manifest: ContextManifest) -> UUID:
        record_id = new_uuid7(now=manifest.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.context_manifest"
                " (id, realm_id, project_id, work_item_id, token_budget, selected, omitted,"
                "  candidate_fingerprint, manifest_digest, compiler_version,"
                "  scoring_policy_digest, compiler_metrics, compiler_metrics_digest,"
                "  compiler_metrics_canonical,manifest_canonical,ranking_snapshot_digest,"
                "  candidate_set_digest,"
                "  grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s,"
                "  %s::jsonb, %s, %s, %s, %s, %s, false, %s)"
                " on conflict (realm_id, project_id, work_item_id, manifest_digest)"
                " do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    manifest.token_budget,
                    canonical_json([item.as_dict() for item in manifest.selected]),
                    canonical_json([item.as_dict() for item in manifest.omitted]),
                    manifest.candidate_fingerprint,
                    manifest.manifest_digest,
                    manifest.compiler_version,
                    manifest.scoring_policy_digest,
                    (
                        None
                        if manifest.compiler_metrics is None
                        else canonical_json(manifest.compiler_metrics.body())
                    ),
                    (
                        None
                        if manifest.compiler_metrics is None
                        else manifest.compiler_metrics.metrics_digest
                    ),
                    (
                        None
                        if manifest.compiler_metrics is None
                        else canonical_json(manifest.compiler_metrics.body())
                    ),
                    canonical_json(manifest.body()) if manifest.compiler_version == 2 else None,
                    manifest.ranking_snapshot_digest,
                    manifest.candidate_set_digest,
                    manifest.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from work.context_manifest"
                " where realm_id = %s and project_id = %s and work_item_id = %s"
                " and manifest_digest = %s",
                (
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    manifest.manifest_digest,
                ),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise ConcurrencyConflict("Context manifest scoped conflict kaydi bulunamadi")
            return UUID(str(existing[0]))

    def store_fragment_set(
        self,
        fragment_set: ContextFragmentSet,
        *,
        created_at: Any,
    ) -> UUID:
        set_id = new_uuid7(now=created_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from work.context_manifest"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and manifest_digest=%s",
                (
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    fragment_set.context_manifest_digest,
                ),
            )
            manifest_row = cursor.fetchone()
            if manifest_row is None:
                raise ConcurrencyConflict("Context fragment manifest exact scope'ta bulunamadi")
            manifest_id = UUID(str(manifest_row[0]))
            cursor.execute(
                "insert into work.context_fragment_set"
                " (id,realm_id,project_id,work_item_id,context_manifest_id,fragment_count,"
                " fragment_set_digest,created_at) values (%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,context_manifest_id) do nothing returning id",
                (
                    set_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    manifest_id,
                    len(fragment_set.fragments),
                    fragment_set.fragment_set_digest,
                    created_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "select id,fragment_set_digest from work.context_fragment_set"
                    " where realm_id=%s and context_manifest_id=%s",
                    (self.realm_id, manifest_id),
                )
                existing = cursor.fetchone()
                if existing is None or str(existing[1]) != fragment_set.fragment_set_digest:
                    raise ConcurrencyConflict("Context fragment set replay digest mismatch")
                return UUID(str(existing[0]))
            for fragment in fragment_set.fragments:
                cursor.execute(
                    "insert into work.context_fragment"
                    " (id,realm_id,project_id,work_item_id,fragment_set_id,context_manifest_id,"
                    " fragment_id,candidate_id,content_kind,role,fragment_order,visibility,"
                    " authority,source_ref,source_revision,content_digest,token_count,required,"
                    " grants_authority,fragment_digest,created_at)"
                    " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    " false,%s,%s)",
                    (
                        new_uuid7(now=created_at),
                        self.realm_id,
                        self.project_id,
                        self.work_item_id,
                        set_id,
                        manifest_id,
                        fragment.fragment_id,
                        fragment.candidate_id,
                        fragment.content_kind.value,
                        fragment.role.value,
                        fragment.order,
                        fragment.visibility.value,
                        int(fragment.authority),
                        fragment.source_ref,
                        fragment.source_revision,
                        fragment.content_digest,
                        fragment.token_count,
                        fragment.required,
                        digest(fragment.body()),
                        created_at,
                    ),
                )
            cursor.execute(
                "select count(*),min(fragment_order),max(fragment_order)"
                " from work.context_fragment where realm_id=%s and fragment_set_id=%s",
                (self.realm_id, set_id),
            )
            count, minimum, maximum = cursor.fetchone()
            if (int(count), int(minimum), int(maximum)) != (
                len(fragment_set.fragments),
                0,
                len(fragment_set.fragments) - 1,
            ):
                raise ConcurrencyConflict("Context fragment persistence exact order mismatch")
        return set_id

    def load_fragment_set(self, set_id: UUID) -> ContextFragmentSet:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select m.manifest_digest,s.fragment_count,s.fragment_set_digest"
                " from work.context_fragment_set s"
                " join work.context_manifest m on m.realm_id=s.realm_id"
                " and m.id=s.context_manifest_id"
                " where s.realm_id=%s and s.project_id=%s and s.work_item_id=%s and s.id=%s",
                (self.realm_id, self.project_id, self.work_item_id, set_id),
            )
            parent = cursor.fetchone()
            if parent is None:
                raise ConcurrencyConflict("Context fragment set exact scope'ta bulunamadi")
            cursor.execute(
                "select fragment_id,candidate_id,content_kind,role,fragment_order,visibility,"
                " authority,source_ref,source_revision,content_digest,token_count,required,"
                " grants_authority from work.context_fragment"
                " where realm_id=%s and fragment_set_id=%s order by fragment_order",
                (self.realm_id, set_id),
            )
            rows = cursor.fetchall()
        if len(rows) != int(parent[1]):
            raise ConcurrencyConflict("Context fragment set kayit sayisi mismatch")
        result = ContextFragmentSet(
            str(parent[0]),
            tuple(
                ContextFragment(
                    fragment_id=str(row[0]),
                    candidate_id=str(row[1]),
                    content_kind=ContextContentKind(str(row[2])),
                    role=ContextRole(str(row[3])),
                    order=int(row[4]),
                    visibility=ContextVisibility(str(row[5])),
                    authority=AuthorityLevel(int(row[6])),
                    source_ref=str(row[7]),
                    source_revision=str(row[8]),
                    content_digest=str(row[9]),
                    token_count=int(row[10]),
                    required=bool(row[11]),
                    grants_authority=bool(row[12]),
                )
                for row in rows
            ),
        )
        if result.fragment_set_digest != str(parent[2]):
            raise ConcurrencyConflict("Context fragment set persisted digest mismatch")
        return result

    def journal_head(self) -> tuple[int, str] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select sequence, entry_digest from work.work_journal_entry"
                " where work_item_id = %s order by sequence desc limit 1",
                (self.work_item_id,),
            )
            row = cursor.fetchone()
        return None if row is None else (int(row[0]), str(row[1]))

    def append_journal(self, entry: JournalEntry, *, expected_head: str | None) -> UUID:
        head = self.journal_head()
        actual = None if head is None else head[1]
        if actual != expected_head or entry.previous_digest != expected_head:
            raise ConcurrencyConflict("WorkJournal optimistic head mismatch")
        record_id = new_uuid7(now=entry.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.work_journal_entry"
                " (id, realm_id, project_id, work_item_id, sequence, event_kind, payload_digest,"
                "  previous_digest, truncated, entry_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    entry.sequence,
                    entry.event_kind,
                    entry.payload_digest,
                    entry.previous_digest,
                    entry.truncated,
                    entry.entry_digest,
                    entry.created_at,
                ),
            )
        return record_id

    def store_checkpoint(
        self, checkpoint: Checkpoint, *, task_plan_id: UUID, job_id: UUID | None = None
    ) -> UUID:
        record_id = new_uuid7(now=checkpoint.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.checkpoint"
                " (id, checkpoint_key, realm_id, project_id, work_item_id, task_plan_id,"
                "  job_id, source_revision, plan_steps, completed_steps, pending_steps,"
                "  step_results, context_manifest_digest,"
                "  journal_head_digest, next_safe_action, checkpoint_digest, grants_authority,"
                "  created_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,"
                "  %s, %s, %s, false, %s)",
                (
                    record_id,
                    checkpoint.checkpoint_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    task_plan_id,
                    job_id,
                    checkpoint.source_revision,
                    list(checkpoint.plan_steps),
                    list(checkpoint.completed_steps),
                    list(checkpoint.pending_steps),
                    canonical_json(dict(checkpoint.step_results)),
                    checkpoint.context_manifest_digest,
                    checkpoint.journal_head_digest,
                    checkpoint.next_safe_action,
                    checkpoint.checkpoint_digest,
                    checkpoint.created_at,
                ),
            )
        return record_id

    def load_resume_bundle(
        self, handoff_digest: str
    ) -> tuple[FinalizedHandoff, ContinuitySnapshot, Checkpoint]:
        """Transcript kullanmadan canonical digest-bound resume bundle yukler."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select h.from_client, h.to_client, h.from_model_ref, h.to_model_ref,"
                " h.snapshot_digest, h.checkpoint_digest, h.source_revision, h.created_at,"
                " s.checkpoint_digest, s.journal_head_digest, s.context_manifest_digest,"
                " s.source_revision, s.first_reads, s.next_safe_actions, s.evidence_refs,"
                " s.created_at, c.checkpoint_key, c.project_id, c.work_item_id, c.task_plan_id,"
                " c.source_revision, c.plan_steps, c.completed_steps, c.pending_steps,"
                " c.step_results,"
                " c.context_manifest_digest, c.journal_head_digest, c.next_safe_action,"
                " c.created_at, h.source_client_capability_digest,"
                " h.target_client_capability_digest,h.source_client_permission_digest,"
                " h.target_client_permission_digest,h.unsupported_capabilities,"
                " h.unsupported_permissions,h.required_replan_items,"
                " h.target_route_decision_id,h.target_route_decision_digest,"
                " h.target_route_valid_until,h.target_route_fresh"
                " from work.finalized_handoff h"
                " join work.continuity_snapshot s"
                " on s.id = h.snapshot_id and s.realm_id = h.realm_id"
                " join work.checkpoint c on c.id = s.checkpoint_id and c.realm_id = s.realm_id"
                " where h.realm_id = %s and h.work_item_id = %s and h.handoff_digest = %s",
                (self.realm_id, self.work_item_id, handoff_digest),
            )
            row = cursor.fetchone()
        if row is None:
            raise ConcurrencyConflict("Finalized handoff bulunamadi veya realm/work mismatch")
        checkpoint = Checkpoint(
            checkpoint_id=str(row[16]),
            project_id=str(row[17]),
            work_item_id=str(row[18]),
            plan_revision_id=str(row[19]),
            source_revision=str(row[20]),
            plan_steps=tuple(row[21]),
            completed_steps=tuple(row[22]),
            pending_steps=tuple(row[23]),
            step_results=tuple(sorted((str(k), str(v)) for k, v in row[24].items())),
            context_manifest_digest=str(row[25]),
            journal_head_digest=str(row[26]),
            next_safe_action=str(row[27]),
            created_at=row[28],
        )
        refs = tuple(
            EvidenceReference(
                kind=str(item["kind"]),
                ref=str(item["ref"]),
                evidence_digest=str(item["digest"]),
                revision=item.get("revision"),
            )
            for item in row[14]
        )
        snapshot = ContinuitySnapshot(
            project_id=str(row[17]),
            work_item_id=str(row[18]),
            checkpoint_digest=str(row[8]),
            journal_head_digest=str(row[9]),
            context_manifest_digest=str(row[10]),
            source_revision=str(row[11]),
            first_reads=tuple(row[12]),
            next_safe_actions=tuple(row[13]),
            evidence_refs=refs,
            created_at=row[15],
        )
        handoff = FinalizedHandoff(
            from_client=str(row[0]),
            to_client=str(row[1]),
            from_model_ref=str(row[2]),
            to_model_ref=str(row[3]),
            snapshot_digest=str(row[4]),
            checkpoint_digest=str(row[5]),
            source_revision=str(row[6]),
            created_at=row[7],
            source_client_capability_digest=row[29],
            target_client_capability_digest=row[30],
            source_client_permission_digest=row[31],
            target_client_permission_digest=row[32],
            unsupported_capabilities=tuple(row[33]),
            unsupported_permissions=tuple(row[34]),
            required_replan_items=tuple(row[35]),
            target_route_decision_id=row[36],
            target_route_decision_digest=row[37],
            target_route_valid_until=row[38],
            target_route_fresh=bool(row[39]),
        )
        if (
            handoff.handoff_digest != handoff_digest
            or snapshot.snapshot_digest != handoff.snapshot_digest
            or checkpoint.checkpoint_digest != handoff.checkpoint_digest
        ):
            raise ConcurrencyConflict("Continuity bundle digest drift")
        return handoff, snapshot, checkpoint

    def store_snapshot(self, snapshot: ContinuitySnapshot, *, checkpoint_id: UUID) -> UUID:
        record_id = new_uuid7(now=snapshot.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.continuity_snapshot"
                " (id, realm_id, project_id, work_item_id, checkpoint_id, checkpoint_digest,"
                "  journal_head_digest, context_manifest_digest, source_revision, first_reads,"
                "  next_safe_actions, evidence_refs, snapshot_digest, grants_authority,"
                "  carries_active_lease, approval_inherited, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,"
                " false, false, false, %s)",
                (
                    record_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    checkpoint_id,
                    snapshot.checkpoint_digest,
                    snapshot.journal_head_digest,
                    snapshot.context_manifest_digest,
                    snapshot.source_revision,
                    list(snapshot.first_reads),
                    list(snapshot.next_safe_actions),
                    canonical_json([item.as_dict() for item in snapshot.evidence_refs]),
                    snapshot.snapshot_digest,
                    snapshot.created_at,
                ),
            )
        return record_id

    def store_handoff(self, handoff: FinalizedHandoff, *, snapshot_id: UUID) -> UUID:
        if handoff.from_client != handoff.to_client and not handoff.cross_client_ready:
            raise PolicyViolation("Cross-client handoff capability ve fresh route kaniti ister")
        record_id = new_uuid7(now=handoff.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.finalized_handoff"
                " (id, realm_id, project_id, work_item_id, snapshot_id, from_client, to_client,"
                "  from_model_ref, to_model_ref, snapshot_digest, checkpoint_digest,"
                "  source_revision, handoff_digest, transcript_included, grants_authority,"
                "  carries_active_lease, approval_inherited, reacquire_required, created_at,"
                "  source_client_capability_digest,target_client_capability_digest,"
                "  source_client_permission_digest,target_client_permission_digest,"
                "  unsupported_capabilities,unsupported_permissions,required_replan_items,"
                "  target_route_decision_id,target_route_decision_digest,"
                "  target_route_valid_until,target_route_fresh)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false,"
                " false, false, false, true, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    snapshot_id,
                    handoff.from_client,
                    handoff.to_client,
                    handoff.from_model_ref,
                    handoff.to_model_ref,
                    handoff.snapshot_digest,
                    handoff.checkpoint_digest,
                    handoff.source_revision,
                    handoff.handoff_digest,
                    handoff.created_at,
                    handoff.source_client_capability_digest,
                    handoff.target_client_capability_digest,
                    handoff.source_client_permission_digest,
                    handoff.target_client_permission_digest,
                    list(handoff.unsupported_capabilities),
                    list(handoff.unsupported_permissions),
                    list(handoff.required_replan_items),
                    handoff.target_route_decision_id,
                    handoff.target_route_decision_digest,
                    handoff.target_route_valid_until,
                    handoff.target_route_fresh,
                ),
            )
        return record_id
