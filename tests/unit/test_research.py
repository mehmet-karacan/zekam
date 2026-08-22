"""P09-T02..T06 kanitli arastirma testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.research_service import (
    ResearchService,
    assert_no_swallowed_results,
    builder_role_count,
    default_dag_nodes,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
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
    ResearchReport,
    ResearchRole,
    RoleOutcome,
    RoleResult,
    SourceKind,
    SourcePolicy,
    SourceSnapshot,
    derive_plan_candidate,
    synthesize,
)

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
INTENT = digest("intent")
CONTENT = digest("content")
REVISION = "9030f6f"


def _policy(**kwargs: object) -> SourcePolicy:
    defaults: dict[str, object] = {
        "allowed_kinds": frozenset({SourceKind.FILE, SourceKind.REPOSITORY}),
        "project_scope": "zekam",
    }
    defaults.update(kwargs)
    return SourcePolicy(**defaults)  # type: ignore[arg-type]


def _budget() -> ResearchBudget:
    return ResearchBudget(max_tokens=1000, max_cost_units=10, max_seconds=300)


def _question(**kwargs: object) -> ResearchQuestion:
    defaults: dict[str, object] = {
        "question_id": "q1",
        "question": "pgvector HNSW filtreli recall nasil olculur?",
        "project_ref": "zekam",
        "work_ref": "ZEKAM-P09-T04",
        "intent_digest": INTENT,
        "source_revision": REVISION,
        "policy": _policy(),
        "budget": _budget(),
        "created_at": NOW,
    }
    defaults.update(kwargs)
    return ResearchQuestion(**defaults)  # type: ignore[arg-type]


def _snapshot(snapshot_id: str = "s1", **kwargs: object) -> SourceSnapshot:
    defaults: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "kind": SourceKind.FILE,
        "locator": "docs/RAG.md",
        "content_digest": CONTENT,
        "captured_at": NOW,
    }
    defaults.update(kwargs)
    return SourceSnapshot(**defaults)  # type: ignore[arg-type]


def _finding(finding_id: str = "f1", snapshot_id: str = "s1") -> Finding:
    return Finding(
        finding_id=finding_id,
        claim="Filtreli recall iterative scan ile olculmelidir",
        citations=(
            Citation(snapshot_id=snapshot_id, locator_detail="line 10-20", content_digest=CONTENT),
        ),
        confidence="high",
    )


def _result(
    role: ResearchRole = ResearchRole.RESEARCHER,
    *,
    agent_ref: str = "agent-a",
    outcome: RoleOutcome = RoleOutcome.SUCCESS,
    findings: tuple[Finding, ...] | None = None,
    blocker: str | None = None,
) -> RoleResult:
    return RoleResult(
        role=role,
        agent_ref=agent_ref,
        outcome=outcome,
        findings=(
            findings
            if findings is not None
            else ((_finding(),) if outcome is RoleOutcome.SUCCESS else ())
        ),
        blocker=blocker,
    )


# -- T02: soru, scope ve butce ------------------------------------------------


def test_soru_scope_disina_cikamaz() -> None:
    with pytest.raises(PolicyViolation):
        _question(policy=_policy(project_scope="baska-proje"))


def test_stale_source_revision_reddedilir() -> None:
    question = _question()
    assert question.is_stale(current_source_revision="yeni", current_intent_digest=INTENT)
    with pytest.raises(PolicyViolation):
        question.assert_current(current_source_revision="yeni", current_intent_digest=INTENT)


def test_stale_intent_digest_reddedilir() -> None:
    question = _question()
    with pytest.raises(PolicyViolation):
        question.assert_current(
            current_source_revision=REVISION, current_intent_digest=digest("baska")
        )


def test_butce_bounded_deliberation_sinirini_asamaz() -> None:
    with pytest.raises(ValidationFailed):
        ResearchBudget(max_tokens=1, max_cost_units=1, max_seconds=601)
    with pytest.raises(ValidationFailed):
        ResearchBudget(max_tokens=1, max_cost_units=1, max_seconds=10, max_rounds=3)
    with pytest.raises(ValidationFailed):
        ResearchBudget(max_tokens=0, max_cost_units=1, max_seconds=10)


def test_https_kaynagi_exact_host_allowlist_ister() -> None:
    with pytest.raises(PolicyViolation):
        SourcePolicy(allowed_kinds=frozenset({SourceKind.HTTPS}))


# -- T03: source snapshot ve provenance ---------------------------------------


def test_snapshot_absolute_path_reddeder() -> None:
    with pytest.raises(PolicyViolation):
        _snapshot(locator="C:/gizli/rapor.md")
    with pytest.raises(PolicyViolation):
        _snapshot(locator="../disari/rapor.md")


def test_repository_snapshot_revision_ister() -> None:
    with pytest.raises(ValidationFailed):
        _snapshot(kind=SourceKind.REPOSITORY, locator="src/zekam/domain/research.py")


def test_https_snapshot_query_string_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        _snapshot(
            kind=SourceKind.HTTPS,
            locator="https://ornek.org/makale?token=abc",
            host="ornek.org",
        )


def test_snapshot_policy_disi_kaynagi_reddeder() -> None:
    snapshot = _snapshot(
        kind=SourceKind.HTTPS, locator="https://ornek.org/makale", host="ornek.org"
    )
    with pytest.raises(PolicyViolation):
        snapshot.assert_permitted(_policy())


def test_snapshot_digest_provenance_korur() -> None:
    first = _snapshot()
    second = _snapshot(locator="docs/BASKA.md")
    assert first.snapshot_digest != second.snapshot_digest
    assert first.snapshot_digest == _snapshot().snapshot_digest


# -- T04: DAG ve subagent -----------------------------------------------------


def _dag() -> ResearchDag:
    return ResearchDag(
        question_id="q1",
        nodes=tuple(
            ResearchNode(node_id=node_id, role=role, depends_on=deps)
            for node_id, role, deps in default_dag_nodes()
        ),
    )


def test_koordinator_subagent_sayilmaz() -> None:
    dag = _dag()
    assert dag.subagent_count == 5
    assert builder_role_count(dag) == 5
    with pytest.raises(PolicyViolation):
        ResearchDag(
            question_id="q1",
            nodes=(ResearchNode(node_id="coordinator", role=ResearchRole.COORDINATOR),),
        )


def test_bagimsiz_roller_paralel_grupta() -> None:
    groups = _dag().parallel_groups()
    assert groups[1] == ("critic", "domain-reviewer", "researcher")
    assert groups[-1] == ("citation-verifier",)


def test_dongulu_dag_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        ResearchDag(
            question_id="q1",
            nodes=(
                ResearchNode(node_id="a", role=ResearchRole.RESEARCHER, depends_on=("b",)),
                ResearchNode(node_id="b", role=ResearchRole.CRITIC, depends_on=("a",)),
            ),
        )


def test_koordinator_child_sonucu_uretemez() -> None:
    with pytest.raises(PolicyViolation):
        _result(ResearchRole.COORDINATOR)


def test_dispatch_koordinatoru_cagirmaz() -> None:
    seen: list[str] = []

    def dispatcher(node_id: str, role: ResearchRole) -> RoleResult:
        seen.append(node_id)
        return _result(role, agent_ref=f"agent-{node_id}")

    report = ResearchService().dispatch(_dag(), dispatcher)
    assert "coordinator" not in seen
    assert len(seen) == 5
    assert report.coordinator_produced_results is False
    assert report.parallel_width == 3


def test_dispatch_rol_uyusmazligini_reddeder() -> None:
    def dispatcher(node_id: str, role: ResearchRole) -> RoleResult:
        return _result(ResearchRole.CRITIC, agent_ref=f"agent-{node_id}")

    with pytest.raises(ValidationFailed):
        ResearchService().dispatch(_dag(), dispatcher)


# -- T05: celiski ve citation verifier ----------------------------------------


def test_kanitsiz_bulgu_kabul_edilmez() -> None:
    with pytest.raises(ValidationFailed):
        Finding(finding_id="f", claim="iddia", citations=(), confidence="high")


def test_direct_contradiction_unresolved_kalir() -> None:
    conflict = Conflict(
        conflict_id="c1",
        kind=ConflictKind.DIRECT_CONTRADICTION,
        left_finding_id="f1",
        right_finding_id="f2",
        detail="iki kaynak zit sonuc veriyor",
    )
    assert conflict.is_unresolved is True
    with pytest.raises(PolicyViolation):
        Conflict(
            conflict_id="c2",
            kind=ConflictKind.DIRECT_CONTRADICTION,
            left_finding_id="f1",
            right_finding_id="f2",
            detail="model uzlasti",
            resolved_by="synthesizer",
        )


def test_verifier_arastirmaciyla_ayni_olamaz() -> None:
    verification = CitationVerification(verifier_ref="agent-a", verified_finding_ids=("f1",))
    with pytest.raises(PolicyViolation):
        verification.assert_independent(frozenset({"agent-a"}))


def test_sentez_non_success_sonucu_yutamaz() -> None:
    results = (
        _result(agent_ref="agent-a"),
        _result(
            ResearchRole.CRITIC,
            agent_ref="agent-b",
            outcome=RoleOutcome.BLOCKED,
            findings=(),
            blocker="kaynak erisilemedi",
        ),
    )
    verification = CitationVerification(verifier_ref="agent-v", verified_finding_ids=("f1",))
    findings, unresolved, non_success = synthesize(results, conflicts=(), verification=verification)
    assert [item.finding_id for item in findings] == ["f1"]
    assert unresolved == ()
    assert [item.outcome for item in non_success] == [RoleOutcome.BLOCKED]


def test_dogrulanmamis_bulgu_rapora_girmez() -> None:
    verification = CitationVerification(
        verifier_ref="agent-v",
        verified_finding_ids=(),
        rejected_finding_ids=("f1",),
        rejection_reasons=("locator dogrulanamadi",),
    )
    findings, _, _ = synthesize((_result(),), conflicts=(), verification=verification)
    assert findings == ()


# -- T06: rapor ve plan candidate ---------------------------------------------


def _pipeline(
    *,
    conflicts: tuple[Conflict, ...] = (),
    outcome: RoleOutcome = RoleOutcome.SUCCESS,
) -> tuple[ResearchService, ResearchReport]:
    service = ResearchService()

    def dispatcher(node_id: str, role: ResearchRole) -> RoleResult:
        if node_id == "critic" and outcome is not RoleOutcome.SUCCESS:
            return _result(
                role, agent_ref=f"agent-{node_id}", outcome=outcome, findings=(), blocker="engel"
            )
        return _result(role, agent_ref=f"agent-{node_id}")

    dispatch = service.dispatch(_dag(), dispatcher)
    report = service.build_report(
        _question(),
        dispatch,
        report_id="r1",
        conflicts=conflicts,
        verification=CitationVerification(
            verifier_ref="agent-verifier", verified_finding_ids=("f1",)
        ),
        snapshots=(_snapshot(),),
    )
    assert_no_swallowed_results(dispatch, report)
    return service, report


def test_temiz_akis_answered_rapor_uretir() -> None:
    _, report = _pipeline()
    assert report.status is ReportStatus.ANSWERED
    assert report.is_actionable is True
    assert report.body()["grants_authority"] is False


def test_unresolved_celiski_raporu_partial_yapar() -> None:
    conflict = Conflict(
        conflict_id="c1",
        kind=ConflictKind.DIRECT_CONTRADICTION,
        left_finding_id="f1",
        right_finding_id="f2",
        detail="zit sonuc",
    )
    _, report = _pipeline(conflicts=(conflict,))
    assert report.status is ReportStatus.PARTIAL
    assert report.is_actionable is False
    assert report.unresolved_conflicts[0].conflict_id == "c1"


def test_blocked_child_raporu_partial_yapar() -> None:
    _, report = _pipeline(outcome=RoleOutcome.BLOCKED)
    assert report.status is ReportStatus.PARTIAL
    assert [item.agent_ref for item in report.non_success_results] == ["agent-critic"]


def test_kanit_yoksa_abstain() -> None:
    service = ResearchService()
    dispatch = service.dispatch(_dag(), lambda node_id, role: _result(role, agent_ref=node_id))
    report = service.build_report(
        _question(),
        dispatch,
        report_id="r2",
        conflicts=(),
        verification=CitationVerification(verifier_ref="agent-v", verified_finding_ids=()),
        snapshots=(_snapshot(),),
    )
    assert report.status is ReportStatus.ABSTAINED
    assert report.findings == ()


def test_actionable_olmayan_rapordan_plan_turetilemez() -> None:
    _, report = _pipeline(outcome=RoleOutcome.BLOCKED)
    with pytest.raises(PolicyViolation):
        derive_plan_candidate(
            report,
            candidate_id="pc1",
            work_ref="ZEKAM-P09-T06",
            source_revision=REVISION,
            proposed_steps=("adim",),
            writable_resources=("path:zekam:docs/RAG.md",),
            acceptance=("test gecer",),
            rollback="git revert",
            risk="medium",
        )


def test_plan_candidate_authority_tasimaz() -> None:
    _, report = _pipeline()
    candidate = ResearchService().to_plan_candidate(
        report,
        candidate_id="pc1",
        work_ref="ZEKAM-P09-T06",
        source_revision=REVISION,
        proposed_steps=("iterative scan olcumu ekle",),
        writable_resources=("path:zekam:docs/RAG.md",),
        acceptance=("recall olcumu testte gecer",),
        rollback="git revert",
        risk="medium",
    )
    body = candidate.body()
    assert body["requires_authorization"] is True
    assert body["approval_inherited"] is False
    assert body["grants_authority"] is False
    assert candidate.report_digest == report.report_digest


def test_plan_candidate_absolute_path_yazamaz() -> None:
    _, report = _pipeline()
    with pytest.raises(PolicyViolation):
        derive_plan_candidate(
            report,
            candidate_id="pc2",
            work_ref="ZEKAM-P09-T06",
            source_revision=REVISION,
            proposed_steps=("adim",),
            writable_resources=("path:zekam:/etc/passwd",),
            acceptance=("test",),
            rollback="revert",
            risk="low",
        )


def test_citation_bilinmeyen_snapshot_reddedilir() -> None:
    service = ResearchService()
    dispatch = service.dispatch(_dag(), lambda node_id, role: _result(role, agent_ref=node_id))
    with pytest.raises(ValidationFailed):
        service.build_report(
            _question(),
            dispatch,
            report_id="r3",
            conflicts=(),
            verification=CitationVerification(verifier_ref="agent-v", verified_finding_ids=("f1",)),
            snapshots=(_snapshot("baska"),),
        )
