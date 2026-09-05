"""P09 arastirma PostgreSQL kabul testleri."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import psycopg
import pytest

from zekam.application.intake_service import IntakeService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.research_service import ResearchService, default_dag_nodes
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.research import (
    Citation,
    CitationVerification,
    Conflict,
    ConflictKind,
    Finding,
    ReportStatus,
    ResearchBudget,
    ResearchDag,
    ResearchNode,
    ResearchQuestion,
    ResearchRole,
    RoleOutcome,
    RoleResult,
    SourceKind,
    SourcePolicy,
    SourceSnapshot,
)
from zekam.domain.work import WorkType
from zekam.infrastructure.postgres.research_repository import ResearchRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
CONTENT = digest("content")


def _setup(connection: Any, realm: Any, tmp_path: Path) -> tuple[Any, Any, ResearchRepository]:
    source = tmp_path / "source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    item = WorkGraphService(connection, realm).create_item(
        project_id=project.id, type=WorkType.RESEARCH, title="Arastirma"
    )
    repository = ResearchRepository(connection, realm.id, project.id, item.id)
    return project, item, repository


def _question(project_slug: str, work_id: str) -> ResearchQuestion:
    return ResearchQuestion(
        question_id="q1",
        question="Filtreli recall nasil olculur?",
        project_ref=project_slug,
        work_ref=work_id,
        intent_digest=digest("intent"),
        source_revision="revision-1",
        policy=SourcePolicy(allowed_kinds=frozenset({SourceKind.FILE}), project_scope=project_slug),
        budget=ResearchBudget(max_tokens=1000, max_cost_units=10, max_seconds=300),
        created_at=NOW,
    )


def _finding(finding_id: str = "f1") -> Finding:
    return Finding(
        finding_id=finding_id,
        claim="Iterative scan filtreli recall'i iyilestirir",
        citations=(Citation(snapshot_id="s1", locator_detail="line 1-5", content_digest=CONTENT),),
        confidence="high",
    )


def _snapshot() -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id="s1",
        kind=SourceKind.FILE,
        locator="docs/RAG.md",
        content_digest=CONTENT,
        captured_at=NOW,
    )


def _dag() -> ResearchDag:
    return ResearchDag(
        question_id="q1",
        nodes=tuple(
            ResearchNode(node_id=node_id, role=role, depends_on=deps)
            for node_id, role, deps in default_dag_nodes()
        ),
    )


def test_arastirma_zinciri_kalici_ve_idempotent(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project, item, repository = _setup(connection, realm, tmp_path)

    intake = IntakeService().resolve(
        f"{project.slug} projesindeki 123 numarali defectin kok nedenini arastir",
        now=NOW,
        projects=(project,),
    )
    first = repository.store_intake(intake.resolution, now=NOW)
    assert repository.store_intake(intake.resolution, now=NOW) == first

    question = _question(project.slug, str(item.id))
    question_id = repository.store_question(question)
    assert repository.store_question(question) == question_id
    repository.store_snapshot(question_id, _snapshot())

    service = ResearchService()
    dispatch = service.dispatch(
        _dag(),
        lambda node_id, role: RoleResult(
            role=role,
            agent_ref=f"agent-{node_id}",
            outcome=RoleOutcome.SUCCESS,
            findings=(_finding(),),
        ),
    )
    for node_id, result in zip(
        [name for group in dispatch.groups for name in group if name != "coordinator"],
        dispatch.results,
        strict=True,
    ):
        repository.store_role_result(question_id, node_id, result, now=NOW)
    assert len(repository.role_results(question_id)) == 5

    report = service.build_report(
        question,
        dispatch,
        report_id="r1",
        conflicts=(),
        verification=CitationVerification(
            verifier_ref="bagimsiz-verifier", verified_finding_ids=("f1",)
        ),
        snapshots=(_snapshot(),),
    )
    assert report.status is ReportStatus.ANSWERED
    report_id = repository.store_report(question_id, report, now=NOW)

    candidate = service.to_plan_candidate(
        report,
        candidate_id="pc1",
        work_ref=str(item.id),
        source_revision="revision-1",
        proposed_steps=("iterative scan olcumu ekle",),
        writable_resources=("path:zekam:docs/RAG.md",),
        acceptance=("recall testi gecer",),
        rollback="git revert",
        risk="medium",
    )
    repository.store_plan_candidate(report_id, candidate, now=NOW)

    with connection.cursor() as cursor:
        cursor.execute(
            "select requires_authorization, approval_inherited, grants_authority"
            " from research.plan_candidate where report_id = %s",
            (report_id,),
        )
        assert cursor.fetchone() == (True, False, False)


def test_subagent_sonucu_olmadan_rapor_yazilamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project, item, repository = _setup(connection, realm, tmp_path)
    question_id = repository.store_question(_question(project.slug, str(item.id)))
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into research.report"
            " (id, realm_id, question_id, status, findings, unresolved_conflicts,"
            "  non_success_results, verifier_ref, verification, report_digest,"
            "  question_digest, grants_authority, created_at)"
            " values (gen_random_uuid(), %s, %s, 'abstained', '[]'::jsonb, '[]'::jsonb,"
            "  '[]'::jsonb, 'v', '{}'::jsonb, %s, %s, false, now())",
            (realm.id, question_id, digest("r"), digest("q")),
        )
    connection.rollback()


def test_answered_rapor_unresolved_celiskiyi_gizleyemez(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project, item, repository = _setup(connection, realm, tmp_path)
    question = _question(project.slug, str(item.id))
    question_id = repository.store_question(question)
    repository.store_role_result(
        question_id,
        "researcher",
        RoleResult(
            role=ResearchRole.RESEARCHER,
            agent_ref="agent-a",
            outcome=RoleOutcome.SUCCESS,
            findings=(_finding(),),
        ),
        now=NOW,
    )
    conflict = Conflict(
        conflict_id="c1",
        kind=ConflictKind.DIRECT_CONTRADICTION,
        left_finding_id="f1",
        right_finding_id="f2",
        detail="zit sonuc",
    )
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into research.report"
            " (id, realm_id, question_id, status, findings, unresolved_conflicts,"
            "  non_success_results, verifier_ref, verification, report_digest,"
            "  question_digest, grants_authority, created_at)"
            " values (gen_random_uuid(), %s, %s, 'answered', %s::jsonb, %s::jsonb,"
            "  '[]'::jsonb, 'v', '{}'::jsonb, %s, %s, false, now())",
            (
                realm.id,
                question_id,
                '[{"finding_id": "f1"}]',
                f'[{{"conflict_id": "{conflict.conflict_id}"}}]',
                digest("r2"),
                question.question_digest,
            ),
        )
    connection.rollback()


def test_verifier_arastirmaciyla_ayni_kimlik_olamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project, item, repository = _setup(connection, realm, tmp_path)
    question = _question(project.slug, str(item.id))
    question_id = repository.store_question(question)
    repository.store_role_result(
        question_id,
        "researcher",
        RoleResult(
            role=ResearchRole.RESEARCHER,
            agent_ref="ayni-kimlik",
            outcome=RoleOutcome.SUCCESS,
            findings=(_finding(),),
        ),
        now=NOW,
    )
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into research.report"
            " (id, realm_id, question_id, status, findings, unresolved_conflicts,"
            "  non_success_results, verifier_ref, verification, report_digest,"
            "  question_digest, grants_authority, created_at)"
            " values (gen_random_uuid(), %s, %s, 'partial', '[]'::jsonb, '[]'::jsonb,"
            "  '[]'::jsonb, 'ayni-kimlik', '{}'::jsonb, %s, %s, false, now())",
            (realm.id, question_id, digest("r3"), question.question_digest),
        )
    connection.rollback()


def test_plan_candidate_yalniz_answered_rapordan_turer(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project, item, repository = _setup(connection, realm, tmp_path)
    question = _question(project.slug, str(item.id))
    question_id = repository.store_question(question)
    repository.store_role_result(
        question_id,
        "researcher",
        RoleResult(
            role=ResearchRole.RESEARCHER,
            agent_ref="agent-a",
            outcome=RoleOutcome.SUCCESS,
            findings=(_finding(),),
        ),
        now=NOW,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into research.report"
            " (id, realm_id, question_id, status, findings, unresolved_conflicts,"
            "  non_success_results, verifier_ref, verification, report_digest,"
            "  question_digest, grants_authority, created_at)"
            " values (gen_random_uuid(), %s, %s, 'abstained', '[]'::jsonb, '[]'::jsonb,"
            "  '[]'::jsonb, 'bagimsiz', '{}'::jsonb, %s, %s, false, now()) returning id",
            (realm.id, question_id, digest("r4"), question.question_digest),
        )
        report_id = cursor.fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into research.plan_candidate"
            " (id, realm_id, report_id, work_item_id, source_revision, proposed_steps,"
            "  writable_resources, acceptance, rollback, risk, open_questions,"
            "  candidate_digest, report_digest, requires_authorization,"
            "  approval_inherited, grants_authority, created_at)"
            " values (gen_random_uuid(), %s, %s, %s, 'revision-1', '[\"adim\"]'::jsonb,"
            "  '[]'::jsonb, '[\"kabul\"]'::jsonb, 'revert', 'low', '[]'::jsonb, %s, %s,"
            "  true, false, false, now())",
            (realm.id, report_id, item.id, digest("pc"), digest("r4")),
        )
    connection.rollback()


def test_arastirma_kayitlari_guncellenemez_ve_silinemez(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project, item, repository = _setup(connection, realm, tmp_path)
    question_id = repository.store_question(_question(project.slug, str(item.id)))
    for table in ("research.question", "research.role_result", "research.plan_candidate"):
        for statement in (f"update {table} set realm_id = realm_id", f"delete from {table}"):
            with (
                pytest.raises(Exception, match=r"append-only|permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
            connection.rollback()
    assert question_id is not None
