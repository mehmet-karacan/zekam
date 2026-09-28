"""Campaign analyse dispatcher bound to ResearchQuestion/ResearchDag contracts.

The dispatcher is intentionally adapter-shaped: production wires it to
ResearchService + OpenCodeResearchAdapter, tests inject a deterministic fake.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from zekam.application.research_service import (
    ResearchService,
    default_dag_nodes,
)
from zekam.domain.canonical import digest
from zekam.domain.research import (
    Citation,
    CitationVerification,
    Conflict,
    Finding,
    ResearchDag,
    ResearchNode,
    ResearchQuestion,
    ResearchRole,
    RoleResult,
    SourceSnapshot,
    synthesize,
)


class AnalyseDispatcherPort(Protocol):
    """Inject boundary between campaign runtime and sub-research execution."""

    def dispatch(
        self,
        question: ResearchQuestion,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> AnalyseResult: ...


@dataclass(frozen=True, slots=True)
class AnalyseResult:
    """Normalized sub-research result for campaign fan-in."""

    question_id: str
    outcome: str
    findings: tuple[Finding, ...]
    verification: CitationVerification | None
    conflicts: tuple[Conflict, ...]
    non_success: tuple[RoleResult, ...]
    measured_tokens: int | None = None
    measured_provider_requests: int | None = None
    measured_latency_ms: float | None = None
    measured_cost_units: float | None = None
    missing_measurement_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "outcome": self.outcome,
            "findings": [f.as_dict() for f in self.findings],
            "verification": None if self.verification is None else self.verification.as_dict(),
            "conflicts": [c.as_dict() for c in self.conflicts],
            "non_success": [r.as_dict() for r in self.non_success],
            "measured_tokens": self.measured_tokens,
            "measured_provider_requests": self.measured_provider_requests,
            "measured_latency_ms": self.measured_latency_ms,
            "measured_cost_units": self.measured_cost_units,
            "missing_measurement_reason": self.missing_measurement_reason,
        }


#: Caller-provided executor for one DAG node.
NodeExecutor = Callable[[str, ResearchRole], RoleResult]


class ResearchServiceAnalyseDispatcher:
    """Wrap the canonical ResearchService to run one campaign sub-question."""

    def __init__(self, executor: NodeExecutor) -> None:
        if not callable(executor):
            raise ValueError("Executor callable olmali")
        self._executor = executor

    def dispatch(
        self,
        question: ResearchQuestion,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> AnalyseResult:
        service = ResearchService()
        service.validate_question(
            question,
            current_source_revision=question.source_revision,
            current_intent_digest=question.intent_digest,
            snapshots=snapshots,
        )
        nodes = tuple(
            ResearchNode(node_id=node_id, role=role, depends_on=deps)
            for node_id, role, deps in default_dag_nodes()
        )
        dag = ResearchDag(question_id=question.question_id, nodes=nodes)
        report = service.dispatch(dag, self._executor)
        findings, unresolved, non_success = synthesize(
            report.results,
            conflicts=(),
            verification=CitationVerification(
                verifier_ref="zekam-verifier:campaign",
                researcher_payload_digest=digest({}),
                evidence_manifest_digest=digest({}),
                verified_finding_ids=(),
                rejected_finding_ids=(),
                rejection_reasons=(),
            ),
            evidence_manifest_digest=digest({}),
        )
        return AnalyseResult(
            question_id=question.question_id,
            outcome="success" if not unresolved and not non_success else "partial",
            findings=findings,
            verification=None,
            conflicts=unresolved,
            non_success=non_success,
        )


class FakeAnalyseDispatcher:
    """Deterministic offline dispatcher for campaign unit tests."""

    def __init__(
        self,
        outcome: str = "success",
        *,
        measured_tokens: int | None = None,
        measured_provider_requests: int | None = None,
    ) -> None:
        if outcome not in {"success", "partial", "failed", "blocked", "abstained"}:
            raise ValueError("Fake dispatcher outcome gecersiz")
        self._outcome = outcome
        self._measured_tokens = measured_tokens
        self._measured_provider_requests = measured_provider_requests

    def dispatch(
        self,
        question: ResearchQuestion,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> AnalyseResult:
        keyword = "pattern" if "pattern" in question.question.lower() else "general"
        citation = Citation(
            snapshot_id=f"snapshot-{keyword}",
            locator_detail="fake-line-1",
            content_digest=digest("fake citation content"),
        )
        finding = Finding(
            finding_id=f"finding-{keyword}",
            claim=f"{keyword} bulgusu (fake)",
            citations=(citation,),
            confidence="medium",
        )
        findings = (finding,) if self._outcome == "success" else ()
        return AnalyseResult(
            question_id=question.question_id,
            outcome=self._outcome,
            findings=findings,
            verification=None,
            conflicts=(),
            non_success=(),
            measured_tokens=self._measured_tokens,
            measured_provider_requests=self._measured_provider_requests,
            missing_measurement_reason=None
            if self._measured_tokens is not None
            else "Fake dispatcher does not emit telemetry; tokens are not estimated",
        )
