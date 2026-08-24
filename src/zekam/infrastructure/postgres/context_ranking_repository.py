"""Canonical assignment/work/source state'ten sealed context provenance uretir."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.context_ranking import (
    ContextCandidateSet,
    ContextCandidateSetIssuer,
    ContextRankingRequest,
    ContextRankingSnapshot,
    ContextRankingSnapshotIssuer,
)
from zekam.application.context_recipe import (
    ContextRecipeRegistry,
    ContextRecipeRole,
    RecipeContextPacket,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    EvidenceReference,
)
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)

_SNAPSHOT_TTL = dt.timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class ContextRankingRepository:
    connection: Any
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID

    def _current_row(self, assignment_id: UUID) -> tuple[Any, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select role,assignment_digest,step_id,plan_id,source_revision,task_terms,"
                " database_now from work.current_context_ranking_projection(%s,%s,%s,%s)",
                (self.realm_id, self.project_id, self.work_item_id, assignment_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Current canonical context ranking assignment bulunamadi")
        return tuple(row)

    def _projection(
        self, assignment_id: UUID
    ) -> tuple[ContextRankingRequest, str, str, dt.datetime]:
        role, assignment_digest, step_id, plan_id, source_revision, task_terms, database_now = (
            self._current_row(assignment_id)
        )
        if str(role) not in {"coordinator", "researcher", "builder", "verifier"}:
            raise PolicyViolation("Assignment role context recipe registry disinda")
        if step_id is None:
            raise PolicyViolation("Context ranking snapshot exact assignment step ister")
        realm_ref = f"realm/{self.realm_id}"
        project_ref = f"project/{self.project_id}"
        work_ref = f"work/{self.work_item_id}"
        step_ref = f"step/{step_id}"
        request = ContextRankingRequest(
            role=str(role),
            target_identity_refs=(work_ref, step_ref),
            step_scope_ref=step_ref,
            work_scope_ref=work_ref,
            project_scope_ref=project_ref,
            realm_scope_ref=realm_ref,
            current_source_revision=str(source_revision),
            compatible_source_revisions=(),
            task_terms=tuple(str(term) for term in task_terms),
            tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
        )
        source_digest = digest(
            {
                "schema": "zekam-context-ranking-source/v1",
                "realm_id": self.realm_id,
                "project_id": self.project_id,
                "work_item_id": self.work_item_id,
                "assignment_id": assignment_id,
                "assignment_digest": str(assignment_digest),
                "plan_id": plan_id,
                "step_id": str(step_id),
                "source_revision": str(source_revision),
            }
        )
        return request, source_digest, str(assignment_digest), database_now

    def issue_current_snapshot(self, assignment_id: UUID) -> ContextRankingSnapshot:
        with self.connection.transaction():
            return self._issue_current_snapshot(assignment_id)

    def _issue_current_snapshot(self, assignment_id: UUID) -> ContextRankingSnapshot:
        request, source_digest, assignment_digest, database_now = self._projection(assignment_id)
        snapshot = ContextRankingSnapshotIssuer.issue(
            request=request,
            realm_ref=request.realm_scope_ref or "",
            project_ref=request.project_scope_ref or "",
            work_ref=request.work_scope_ref or "",
            step_ref=request.step_scope_ref or "",
            assignment_id=str(assignment_id),
            assignment_digest=assignment_digest,
            source_snapshot_digest=source_digest,
            captured_at=database_now,
            expires_at=database_now + _SNAPSHOT_TTL,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.context_ranking_snapshot"
                " (realm_id,project_id,work_item_id,assignment_id,assignment_digest,"
                " source_snapshot_digest,snapshot_digest,canonical_body,captured_at,expires_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                (
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    assignment_id,
                    snapshot.assignment_digest,
                    snapshot.source_snapshot_digest,
                    snapshot.snapshot_digest,
                    canonical_json(snapshot.body()),
                    snapshot.captured_at,
                    snapshot.expires_at,
                ),
            )
        return snapshot

    def assert_current_snapshot(self, snapshot: ContextRankingSnapshot) -> None:
        request, source_digest, assignment_digest, database_now = self._projection(
            UUID(snapshot.assignment_id)
        )
        ContextRankingSnapshotIssuer.verify(snapshot, now=database_now)
        if (
            request != snapshot.request
            or source_digest != snapshot.source_snapshot_digest
            or assignment_digest != snapshot.assignment_digest
        ):
            raise PolicyViolation("Context ranking snapshot canonical state drift")

    def issue_candidate_set(
        self,
        snapshot: ContextRankingSnapshot,
    ) -> ContextCandidateSet:
        with self.connection.transaction():
            return self._issue_candidate_set(snapshot)

    def _issue_candidate_set(
        self,
        snapshot: ContextRankingSnapshot,
    ) -> ContextCandidateSet:
        self.assert_current_snapshot(snapshot)
        candidates, contents = self._current_candidate_material(snapshot)
        database_now = self._current_row(UUID(snapshot.assignment_id))[6]
        candidate_set = ContextCandidateSetIssuer.issue(
            snapshot, candidates, contents, now=database_now
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.context_candidate_set"
                " (realm_id,project_id,work_item_id,ranking_snapshot_digest,"
                " candidate_set_digest,candidate_fingerprint,candidate_count,candidate_tokens,"
                " canonical_body,captured_at,expires_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                (
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    snapshot.snapshot_digest,
                    candidate_set.candidate_set_digest,
                    candidate_set.candidate_fingerprint,
                    len(candidate_set.candidates),
                    sum(item.token_count for item in candidate_set.candidates),
                    canonical_json(candidate_set.body()),
                    candidate_set.captured_at,
                    candidate_set.expires_at,
                ),
            )
        return candidate_set

    def _current_candidate_material(
        self, snapshot: ContextRankingSnapshot
    ) -> tuple[tuple[ContextCandidate, ...], dict[str, str]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,entity_type,candidate_kind,payload,payload_digest,recorded_at"
                " from work.current_context_source_revisions(%s,%s)",
                (self.realm_id, self.work_item_id),
            )
            rows = cursor.fetchall()
        required_kinds = {
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
            ContextCandidateKind.RUN_STATUS,
        }
        available_kinds = {ContextCandidateKind(str(row[2])) for row in rows}
        if not required_kinds <= available_kinds:
            missing = ",".join(sorted(item.value for item in required_kinds - available_kinds))
            raise PolicyViolation(f"Context candidate canonical source eksik: {missing}")
        candidates: list[ContextCandidate] = []
        contents: dict[str, str] = {}
        for revision_id, _entity_type, candidate_kind, payload, payload_digest, recorded_at in rows:
            content = canonical_json(payload)
            if digest(payload) != str(payload_digest):
                raise PolicyViolation("Context canonical revision payload digest drift")
            kind = ContextCandidateKind(str(candidate_kind))
            candidate_id = f"revision/{revision_id}/{kind.value}"
            contents[candidate_id] = content
            candidates.append(
                ContextCandidate(
                    candidate_id=candidate_id,
                    authority=AuthorityLevel.CANONICAL,
                    observed_at=recorded_at,
                    source_revision=(
                        snapshot.request.current_source_revision or str(payload_digest)
                    ),
                    content_digest=digest(content),
                    token_count=len(content.encode("utf-8")),
                    kind=kind,
                    source_ref=f"revision/{revision_id}",
                    identity_refs=snapshot.request.target_identity_refs,
                    scope_ref=snapshot.request.work_scope_ref or snapshot.work_ref,
                    applicable_roles=(snapshot.request.role,),
                    evidence_refs=(
                        EvidenceReference("work", f"revision/{revision_id}", str(payload_digest)),
                    ),
                    canonical_revision_id=str(revision_id),
                )
            )
        return tuple(candidates), contents

    def assert_current_context(
        self, snapshot: ContextRankingSnapshot, candidate_set: ContextCandidateSet
    ) -> None:
        self.assert_current_snapshot(snapshot)
        current_candidates, _ = self._current_candidate_material(snapshot)
        current_fingerprint = digest(
            [
                item.candidate_digest
                for item in sorted(current_candidates, key=lambda row: row.candidate_id)
            ]
        )
        if current_fingerprint != candidate_set.candidate_fingerprint:
            raise PolicyViolation("Context candidate set canonical source revision stale")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from work.context_candidate_set"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and ranking_snapshot_digest=%s and candidate_set_digest=%s"
                " and candidate_fingerprint=%s and expires_at>clock_timestamp()",
                (
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    snapshot.snapshot_digest,
                    candidate_set.candidate_set_digest,
                    candidate_set.candidate_fingerprint,
                ),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Context candidate set canonical provenance bulunamadi")

    def compile_current(
        self,
        snapshot: ContextRankingSnapshot,
        candidate_set: ContextCandidateSet,
        *,
        role: ContextRecipeRole,
        token_budget: int,
        minimum_authority: AuthorityLevel,
    ) -> RecipeContextPacket:
        """Current lock, compile ve manifest insert'i tek transaction'da tamamlar."""

        with self.connection.transaction():
            self.assert_current_context(snapshot, candidate_set)
            database_now = self._current_row(UUID(snapshot.assignment_id))[6]
            packet = ContextRecipeRegistry().compile(
                role,
                candidate_set,
                token_budget=token_budget,
                minimum_authority=minimum_authority,
                now=database_now,
                ranking_snapshot=snapshot,
            )
            ContextContinuityRepository(
                self.connection, self.realm_id, self.project_id, self.work_item_id
            ).store_manifest(packet.manifest)
        return packet
