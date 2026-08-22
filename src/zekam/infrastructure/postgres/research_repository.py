"""PostgreSQL intake ve arastirma append-only repository."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.identifiers import new_uuid7
from zekam.domain.intake import IntakeResolution
from zekam.domain.research import (
    CitationVerification,
    PlanCandidate,
    ResearchQuestion,
    ResearchReport,
    RoleResult,
    SourceSnapshot,
)


@dataclass(frozen=True, slots=True)
class ResearchRepository:
    """Realm ve proje kapsamli arastirma kayitlari."""

    connection: Any
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID

    # -- intake ---------------------------------------------------------------

    def store_intake(self, resolution: IntakeResolution, *, now: dt.datetime) -> UUID:
        record_id = new_uuid7(now=now)
        body = resolution.body()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into research.intake_resolution"
                " (id, realm_id, project_id, request_class, request_digest, resolution_digest,"
                "  exact_identifiers, project_candidates, ambiguities, subject_used,"
                "  anaphora_present, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,"
                "  false, %s)"
                " on conflict (realm_id, resolution_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    self.project_id if resolution.project_ref else None,
                    str(resolution.request_class),
                    resolution.request_digest,
                    resolution.resolution_digest,
                    canonical_json(body["exact_identifiers"]),
                    canonical_json(body["project_candidates"]),
                    canonical_json(body["ambiguities"]),
                    resolution.subject_used,
                    resolution.anaphora_present,
                    now,
                ),
            )
            return self._resolve_id(
                cursor,
                "research.intake_resolution",
                "resolution_digest",
                resolution.resolution_digest,
            )

    # -- question ve kaynak ---------------------------------------------------

    def store_question(self, question: ResearchQuestion) -> UUID:
        record_id = new_uuid7(now=question.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into research.question"
                " (id, realm_id, project_id, work_item_id, question, intent_digest,"
                "  source_revision, source_policy, budget, question_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)"
                " on conflict (realm_id, question_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    self.project_id,
                    self.work_item_id,
                    question.question,
                    question.intent_digest,
                    question.source_revision,
                    canonical_json(question.policy.as_dict()),
                    canonical_json(question.budget.as_dict()),
                    question.question_digest,
                    question.created_at,
                ),
            )
            return self._resolve_id(
                cursor, "research.question", "question_digest", question.question_digest
            )

    def store_snapshot(self, question_id: UUID, snapshot: SourceSnapshot) -> UUID:
        record_id = new_uuid7(now=snapshot.captured_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into research.source_snapshot"
                " (id, realm_id, question_id, kind, locator, host, revision, content_digest,"
                "  snapshot_digest, captured_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, snapshot_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    question_id,
                    str(snapshot.kind),
                    snapshot.locator,
                    snapshot.host,
                    snapshot.revision,
                    snapshot.content_digest,
                    snapshot.snapshot_digest,
                    snapshot.captured_at,
                ),
            )
            return self._resolve_id(
                cursor, "research.source_snapshot", "snapshot_digest", snapshot.snapshot_digest
            )

    # -- child sonuclari ------------------------------------------------------

    def store_role_result(
        self, question_id: UUID, node_id: str, result: RoleResult, *, now: dt.datetime
    ) -> UUID:
        record_id = new_uuid7(now=now)
        body = result.as_dict()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into research.role_result"
                " (id, realm_id, question_id, node_id, role, agent_ref, outcome, findings,"
                "  objections, blocker, result_digest, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, false, %s)"
                " on conflict (realm_id, question_id, node_id) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    question_id,
                    node_id,
                    str(result.role),
                    result.agent_ref,
                    str(result.outcome),
                    canonical_json(body["findings"]),
                    canonical_json(body["objections"]),
                    result.blocker,
                    result.result_digest,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from research.role_result where question_id = %s and node_id = %s",
                (question_id, node_id),
            )
            return UUID(str(cursor.fetchone()[0]))

    def role_results(self, question_id: UUID) -> tuple[tuple[str, str, str], ...]:
        """(node_id, role, outcome) uclusu; gorunurluk kontrolu icin."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select node_id, role, outcome from research.role_result"
                " where question_id = %s order by node_id",
                (question_id,),
            )
            return tuple((str(row[0]), str(row[1]), str(row[2])) for row in cursor.fetchall())

    # -- rapor ve plan candidate ---------------------------------------------

    def store_report(self, question_id: UUID, report: ResearchReport, *, now: dt.datetime) -> UUID:
        record_id = new_uuid7(now=now)
        body = report.body()
        verification: CitationVerification = report.verification
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into research.report"
                " (id, realm_id, question_id, status, findings, unresolved_conflicts,"
                "  non_success_results, verifier_ref, verification, report_digest,"
                "  question_digest, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb,"
                "  %s, %s, false, %s)"
                " on conflict (realm_id, report_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    question_id,
                    str(report.status),
                    canonical_json(body["findings"]),
                    canonical_json(body["unresolved_conflicts"]),
                    canonical_json(body["non_success_results"]),
                    verification.verifier_ref,
                    canonical_json(verification.as_dict()),
                    report.report_digest,
                    report.question_digest,
                    now,
                ),
            )
            return self._resolve_id(
                cursor, "research.report", "report_digest", report.report_digest
            )

    def store_plan_candidate(
        self, report_id: UUID, candidate: PlanCandidate, *, now: dt.datetime
    ) -> UUID:
        record_id = new_uuid7(now=now)
        body = candidate.body()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into research.plan_candidate"
                " (id, realm_id, report_id, work_item_id, source_revision, proposed_steps,"
                "  writable_resources, acceptance, rollback, risk, open_questions,"
                "  candidate_digest, report_digest, requires_authorization,"
                "  approval_inherited, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,"
                "  %s::jsonb, %s, %s, true, false, false, %s)"
                " on conflict (realm_id, candidate_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    report_id,
                    self.work_item_id,
                    candidate.source_revision,
                    canonical_json(body["proposed_steps"]),
                    canonical_json(body["writable_resources"]),
                    canonical_json(body["acceptance"]),
                    candidate.rollback,
                    candidate.risk,
                    canonical_json(body["open_questions"]),
                    candidate.candidate_digest,
                    candidate.report_digest,
                    now,
                ),
            )
            return self._resolve_id(
                cursor, "research.plan_candidate", "candidate_digest", candidate.candidate_digest
            )

    # -- yardimci -------------------------------------------------------------

    @staticmethod
    def _resolve_id(cursor: Any, table: str, column: str, value: str) -> UUID:
        """Insert satiri dondurmediyse mevcut idempotent kaydin kimligini okur."""

        row = cursor.fetchone()
        if row is not None:
            return UUID(str(row[0]))
        cursor.execute(f"select id from {table} where {column} = %s", (value,))
        return UUID(str(cursor.fetchone()[0]))
