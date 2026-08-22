"""P09 uctan uca: dogal dil istegi -> arastirma -> rapor -> plan candidate.

Zincirin hicbir adimi authority uretmez. Plan candidate hala exact authorization
ister; bu test bunu kanonik store uzerinde dogrular.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from zekam.application.intake_service import IntakeService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.research_service import (
    ResearchService,
    assert_no_swallowed_results,
    default_dag_nodes,
)
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.intake import RequestClass
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

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
CONTENT = digest("content")


def _snapshot(snapshot_id: str = "s1") -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=snapshot_id,
        kind=SourceKind.REPOSITORY,
        locator="docs/RAG.md",
        revision="revision-1",
        content_digest=CONTENT,
        captured_at=NOW,
    )


def _finding(finding_id: str, claim: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        claim=claim,
        citations=(
            Citation(snapshot_id="s1", locator_detail="line 12-30", content_digest=CONTENT),
        ),
        confidence="high",
    )


def test_dogal_dilden_plan_candidate_e2e(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    source = tmp_path / "gpu"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source, slug="gpu")
    item = WorkGraphService(connection, realm).create_item(
        project_id=project.id, type=WorkType.RESEARCH, title="Filtreli recall arastirmasi"
    )
    repository = ResearchRepository(connection, realm.id, project.id, item.id)

    # 1. Dogal dil istegi: sinif, proje ve exact kimlik cozulur.
    outcome = IntakeService().resolve(
        "gpu projesindeki filtreli recall dususunun kok nedenini arastir",
        now=NOW,
        projects=(project,),
    )
    assert outcome.resolution.request_class is RequestClass.RESEARCH
    assert outcome.resolution.project_ref == "gpu"
    assert outcome.may_start_work is True
    repository.store_intake(outcome.resolution, now=NOW)

    # 2. Scope ve butceye bagli soru.
    question = ResearchQuestion(
        question_id="q1",
        question="Filtreli recall dususunun kok nedeni nedir?",
        project_ref="gpu",
        work_ref=str(item.id),
        intent_digest=digest("intent"),
        source_revision="revision-1",
        policy=SourcePolicy(allowed_kinds=frozenset({SourceKind.REPOSITORY}), project_scope="gpu"),
        budget=ResearchBudget(max_tokens=20_000, max_cost_units=50, max_seconds=600),
        created_at=NOW,
    )
    service = ResearchService()
    service.validate_question(
        question,
        current_source_revision="revision-1",
        current_intent_digest=digest("intent"),
        snapshots=(_snapshot(),),
    )
    question_id = repository.store_question(question)
    repository.store_snapshot(question_id, _snapshot())

    # 3. En az bir gercek subagent; koordinator sayilmaz.
    dag = ResearchDag(
        question_id="q1",
        nodes=tuple(
            ResearchNode(node_id=node_id, role=role, depends_on=deps)
            for node_id, role, deps in default_dag_nodes()
        ),
    )
    claims = {
        "researcher": ("f1", "HNSW filtreli aramada iterative scan kapali"),
        "domain-reviewer": ("f2", "Partial index secimi filtre kardinalitesine bagli"),
        "critic": ("f3", "Olcum tek sorgu setiyle yapilmis"),
        "synthesizer": ("f4", "Iterative scan acilmali ve recall yeniden olculmeli"),
        "citation-verifier": ("f5", "Butun locator'lar snapshot ile eslesiyor"),
    }

    def dispatcher(node_id: str, role: ResearchRole) -> RoleResult:
        finding_id, claim = claims[node_id]
        return RoleResult(
            role=role,
            agent_ref=f"agent-{node_id}",
            outcome=RoleOutcome.SUCCESS,
            findings=(_finding(finding_id, claim),),
        )

    dispatch = service.dispatch(dag, dispatcher)
    assert dispatch.subagent_count == 5
    assert dispatch.coordinator_produced_results is False
    for node_id, result in zip(
        [name for group in dispatch.groups for name in group if name != "coordinator"],
        dispatch.results,
        strict=True,
    ):
        repository.store_role_result(question_id, node_id, result, now=NOW)

    # 4. Bagimsiz citation verifier; arastirmacilardan farkli kimlik.
    verification = CitationVerification(
        verifier_ref="bagimsiz-verifier",
        verified_finding_ids=("f1", "f2", "f4"),
        rejected_finding_ids=("f3", "f5"),
        rejection_reasons=("tek sorgu seti kanit degil", "kendi kendini dogrulama"),
    )
    report = service.build_report(
        question,
        dispatch,
        report_id="r1",
        conflicts=(),
        verification=verification,
        snapshots=(_snapshot(),),
    )
    assert_no_swallowed_results(dispatch, report)
    assert report.status is ReportStatus.ANSWERED
    assert {item.finding_id for item in report.findings} == {"f1", "f2", "f4"}
    report_id = repository.store_report(question_id, report, now=NOW)

    # 5. Plan candidate: authority yok, exact authorization sart.
    candidate = service.to_plan_candidate(
        report,
        candidate_id="pc1",
        work_ref=str(item.id),
        source_revision="revision-1",
        proposed_steps=(
            "iterative scan ayarini ac",
            "filtreli recall olcum harness'ini ekle",
        ),
        writable_resources=("path:gpu:docs/RAG.md",),
        acceptance=("recall olcum testi gecer",),
        rollback="git revert ile onceki revision'a don",
        risk="medium",
    )
    repository.store_plan_candidate(report_id, candidate, now=NOW)

    with connection.cursor() as cursor:
        cursor.execute(
            "select requires_authorization, approval_inherited, grants_authority,"
            " jsonb_array_length(proposed_steps)"
            " from research.plan_candidate where realm_id = %s and report_id = %s",
            (realm.id, report_id),
        )
        assert cursor.fetchone() == (True, False, False, 2)
        cursor.execute(
            "select count(*) from research.role_result where question_id = %s", (question_id,)
        )
        assert cursor.fetchone()[0] == 5


def test_celiski_cozulmeden_plan_uretilemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    source = tmp_path / "gpu"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source, slug="gpu")
    item = WorkGraphService(connection, realm).create_item(
        project_id=project.id, type=WorkType.RESEARCH, title="Celiskili arastirma"
    )
    question = ResearchQuestion(
        question_id="q2",
        question="Hangi index profili dogru?",
        project_ref="gpu",
        work_ref=str(item.id),
        intent_digest=digest("intent"),
        source_revision="revision-1",
        policy=SourcePolicy(allowed_kinds=frozenset({SourceKind.REPOSITORY}), project_scope="gpu"),
        budget=ResearchBudget(max_tokens=1000, max_cost_units=10, max_seconds=120),
        created_at=NOW,
    )
    service = ResearchService()
    dag = ResearchDag(
        question_id="q2",
        nodes=(
            ResearchNode(node_id="researcher", role=ResearchRole.RESEARCHER),
            ResearchNode(node_id="critic", role=ResearchRole.CRITIC),
        ),
    )
    dispatch = service.dispatch(
        dag,
        lambda node_id, role: RoleResult(
            role=role,
            agent_ref=f"agent-{node_id}",
            outcome=RoleOutcome.SUCCESS,
            findings=(_finding("f1" if node_id == "researcher" else "f2", f"{node_id} iddiasi"),),
        ),
    )
    report = service.build_report(
        question,
        dispatch,
        report_id="r2",
        conflicts=(
            Conflict(
                conflict_id="c1",
                kind=ConflictKind.DIRECT_CONTRADICTION,
                left_finding_id="f1",
                right_finding_id="f2",
                detail="iki bulgu zit index profili oneriyor",
            ),
        ),
        verification=CitationVerification(
            verifier_ref="bagimsiz", verified_finding_ids=("f1", "f2")
        ),
        snapshots=(_snapshot(),),
    )
    assert report.status is ReportStatus.PARTIAL
    assert report.unresolved_conflicts[0].is_unresolved is True
    assert report.is_actionable is False
    connection.rollback()
