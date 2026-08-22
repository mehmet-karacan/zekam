"""Research DAG dispatch, sentez ve plan candidate uretimi.

Koordinator yalniz planlar ve fan-in yapar; child isi kendisi yapamaz. Her child
strict envelope dondurur. Rapor authority uretmez; plan candidate hala exact
authorization ister.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.research import (
    BUILDER_ROLES,
    CitationVerification,
    Conflict,
    Finding,
    PlanCandidate,
    ReportStatus,
    ResearchDag,
    ResearchQuestion,
    ResearchReport,
    ResearchRole,
    RoleOutcome,
    RoleResult,
    SourceSnapshot,
    derive_plan_candidate,
    synthesize,
)

#: Koordinator disindaki her node icin bir dispatcher cagrilir.
Dispatcher = Callable[[str, ResearchRole], RoleResult]


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """DAG yurutmesinin gozlemlenebilir sonucu."""

    question_id: str
    subagent_count: int
    coordinator_produced_results: bool
    groups: tuple[tuple[str, ...], ...]
    results: tuple[RoleResult, ...]

    def __post_init__(self) -> None:
        if self.coordinator_produced_results:
            raise PolicyViolation("koordinator child sonucu uretemez")

    @property
    def parallel_width(self) -> int:
        return max((len(group) for group in self.groups), default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "subagent_count": self.subagent_count,
            "coordinator_produced_results": False,
            "groups": [list(group) for group in self.groups],
            "parallel_width": self.parallel_width,
            "results": [item.as_dict() for item in self.results],
        }


@dataclass(frozen=True, slots=True)
class ResearchService:
    """Salt orchestration; kanonik kayit yazmaz."""

    def validate_question(
        self,
        question: ResearchQuestion,
        *,
        current_source_revision: str,
        current_intent_digest: str,
        snapshots: tuple[SourceSnapshot, ...] = (),
    ) -> None:
        """Scope, staleness ve source policy kapilarini birlikte uygular."""

        question.assert_current(
            current_source_revision=current_source_revision,
            current_intent_digest=current_intent_digest,
        )
        for snapshot in snapshots:
            snapshot.assert_permitted(question.policy)

    def dispatch(self, dag: ResearchDag, dispatcher: Dispatcher) -> DispatchReport:
        """DAG'i topolojik gruplar halinde yurutur.

        Bagimsiz ilk roller ayni grupta yer alir; cagiran taraf grubu paralel
        yurutebilir. Koordinator node'u dispatcher'a hic verilmez.
        """

        roles = {node.node_id: node.role for node in dag.nodes}
        groups = dag.parallel_groups()
        results: list[RoleResult] = []
        for group in groups:
            for node_id in group:
                role = roles[node_id]
                if role is ResearchRole.COORDINATOR:
                    continue
                result = dispatcher(node_id, role)
                if result.role is not role:
                    raise ValidationFailed("child sonucu atanan rolle uyusmuyor")
                results.append(result)
        if not results:
            raise PolicyViolation("agentic arastirma en az bir child sonucu ister")
        return DispatchReport(
            question_id=dag.question_id,
            subagent_count=dag.subagent_count,
            coordinator_produced_results=False,
            groups=groups,
            results=tuple(results),
        )

    def build_report(
        self,
        question: ResearchQuestion,
        dispatch: DispatchReport,
        *,
        report_id: str,
        conflicts: tuple[Conflict, ...],
        verification: CitationVerification,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> ResearchReport:
        """Fan-in sonucunu rapora cevirir; durum kanitla belirlenir."""

        if dispatch.question_id != question.question_id:
            raise ValidationFailed("dispatch baska bir soruya ait")
        findings, unresolved, non_success = synthesize(
            dispatch.results,
            conflicts=conflicts,
            verification=verification,
        )
        status = self._status(findings, unresolved, non_success)
        return ResearchReport(
            report_id=report_id,
            question_id=question.question_id,
            question_digest=question.question_digest,
            findings=findings,
            unresolved_conflicts=unresolved,
            non_success_results=non_success,
            verification=verification,
            snapshots=snapshots,
            status=status,
        )

    @staticmethod
    def _status(
        findings: tuple[Finding, ...],
        unresolved: tuple[Conflict, ...],
        non_success: tuple[RoleResult, ...],
    ) -> ReportStatus:
        if not findings:
            return ReportStatus.ABSTAINED
        if unresolved or non_success:
            return ReportStatus.PARTIAL
        return ReportStatus.ANSWERED

    def to_plan_candidate(
        self,
        report: ResearchReport,
        *,
        candidate_id: str,
        work_ref: str,
        source_revision: str,
        proposed_steps: tuple[str, ...],
        writable_resources: tuple[str, ...],
        acceptance: tuple[str, ...],
        rollback: str,
        risk: str,
    ) -> PlanCandidate:
        return derive_plan_candidate(
            report,
            candidate_id=candidate_id,
            work_ref=work_ref,
            source_revision=source_revision,
            proposed_steps=proposed_steps,
            writable_resources=writable_resources,
            acceptance=acceptance,
            rollback=rollback,
            risk=risk,
        )


def default_dag_nodes() -> tuple[tuple[str, ResearchRole, tuple[str, ...]], ...]:
    """Kanonik baslangic rol DAG'i.

    Ilk bagimsiz roller (researcher, domain-reviewer, critic) paraleldir;
    synthesizer onlari bekler, citation-verifier sentezden sonra gelir.
    """

    return (
        ("coordinator", ResearchRole.COORDINATOR, ()),
        ("researcher", ResearchRole.RESEARCHER, ("coordinator",)),
        ("domain-reviewer", ResearchRole.DOMAIN_REVIEWER, ("coordinator",)),
        ("critic", ResearchRole.CRITIC, ("coordinator",)),
        (
            "synthesizer",
            ResearchRole.SYNTHESIZER,
            ("researcher", "domain-reviewer", "critic"),
        ),
        ("citation-verifier", ResearchRole.CITATION_VERIFIER, ("synthesizer",)),
    )


def assert_no_swallowed_results(dispatch: DispatchReport, report: ResearchReport) -> None:
    """Non-success child sonucunun raporda gorunur kaldigini dogrular."""

    expected = {
        item.result_digest for item in dispatch.results if item.outcome is not RoleOutcome.SUCCESS
    }
    present = {item.result_digest for item in report.non_success_results}
    if expected - present:
        raise PolicyViolation("non-success child sonucu raporda kaybolamaz")


def builder_role_count(dag: ResearchDag) -> int:
    """Koordinator sayilmadan gercek subagent sayisi."""

    return sum(1 for node in dag.nodes if node.role in BUILDER_ROLES)
