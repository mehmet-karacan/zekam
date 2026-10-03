"""Unit-test dongusu icin agent rolleri ve canonical dispatch baglantisi (W05, D13).

Yeni global rol/enum yoktur: analyzer+planner ``RESEARCHER``, test developer ``BUILDER``,
bagimsiz inceleme ``VERIFIER`` rolunu kullanir; gorev uzmanligi yalniz context manifest
metadata'sidir. Uretim yolu ``CanonicalAgentDispatchService`` (assignment-first: assignment
ve invocation effect'ten ONCE kalicilanir, terminal sonuc digest'i baglanir). Talimat ve
bounded context icerigi nesne deposuna yazilir; assignment yalniz digest'lerini tasir.

Model ciktisi strict ``AgentResultEnvelope`` + role ozgu ``body``'dir; internal UUID/manifest
alanlarini model uydurmaz, harness uretir. Ham dusunce/transcript saklanmaz; context secret
benzeri icerik tasiyamaz.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID, uuid5

from zekam.application.agent_dispatch import AssignmentStore, CanonicalAgentDispatchService
from zekam.application.object_store import ObjectStore
from zekam.application.unit_test_patch import TestPatch, parse_patch
from zekam.application.unit_test_plan import (
    BehaviorScenario,
    PlanDelta,
    parse_plan_delta,
    parse_scenario,
)
from zekam.application.unit_test_plateau import StrategyKind
from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.envelope import (
    AgentResultEnvelope,
    AgentRole,
    EnvelopeEvidence,
    EnvelopeStatus,
)
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.unit_test_engineering import UnitTestRequest
from zekam.infrastructure.clients.adapters import ClientAdapter

_NAMESPACE: Final = UUID("7d1c0c7e-4f0e-5a52-9a43-3f4b9a0c1e11")
MAX_ARTIFACT_BYTES: Final = 1_000_000


class AgentSpecialty(StrEnum):
    ANALYZER_PLANNER = "analyzer-planner"
    BUILDER = "builder"
    VERIFIER = "verifier"
    DEFECT_TRIAGE = "defect-triage"
    FINAL_VERIFIER = "final-verifier"


ROLE_FOR: Final[Mapping[AgentSpecialty, AssignmentRole]] = {
    AgentSpecialty.ANALYZER_PLANNER: AssignmentRole.RESEARCHER,
    AgentSpecialty.BUILDER: AssignmentRole.BUILDER,
    AgentSpecialty.VERIFIER: AssignmentRole.VERIFIER,
    AgentSpecialty.DEFECT_TRIAGE: AssignmentRole.VERIFIER,
    AgentSpecialty.FINAL_VERIFIER: AssignmentRole.VERIFIER,
}
VERIFYING: Final = frozenset(
    {AgentSpecialty.VERIFIER, AgentSpecialty.DEFECT_TRIAGE, AgentSpecialty.FINAL_VERIFIER}
)

_INSTRUCTIONS: Final[Mapping[AgentSpecialty, str]] = {
    AgentSpecialty.ANALYZER_PLANNER: (
        "Read-only analyzer/planner. Produce body.plan with behavior scenarios (scenario id, risk,"
        " oracle kind/ref, priority, method, acceptance). No test-count quota. Unresolved oracle ->"
        " oracle_kind=unresolved-assumption. Report body.context_gaps and"
        " body.testability_findings."
    ),
    AgentSpecialty.BUILDER: (
        "Test developer. Produce body.patch (test files only), body.links (scenario_id,test_ref),"
        " body.approach (strategy,note), optional body.plan_delta. Never edit production, POM,"
        " coverage config or verifier assets. Never copy production output into expected values."
    ),
    AgentSpecialty.VERIFIER: (
        "Independent verifier of a builder result. Produce body.verdict, scenario_results,"
        " quality_findings, new_material_risks. You are not the builder of this candidate."
    ),
    AgentSpecialty.DEFECT_TRIAGE: (
        "Independent defect triage. Decide body.defect_review.finding: defect-confirmed,"
        " test-wrong or inconclusive, citing the oracle. Do not fix production."
    ),
    AgentSpecialty.FINAL_VERIFIER: (
        "Independent final verifier. Check diff, oracle, quality, scope and remaining material"
        " risk of the final candidate. Produce body.verdict and new_material_risks."
    ),
}


_FORMAT_NOTE: Final = (
    " Return JSON payload {result:{agent_id,status,summary,evidence:[{kind,reference}]},"
    " body:{...}}; no raw reasoning, no secrets."
)


class AgentDispatchFailure(ZekamError):
    """Agent sonucu kullanilabilir degil; ``category`` makine-okunur nedendir."""

    code = "unit-test-agent-failure"

    def __init__(
        self, category: str, blockers: tuple[str, ...] = (), *, recoverable: bool = False
    ) -> None:
        super().__init__(f"Agent dispatch basarisiz: {category}")
        self.category = category
        self.blockers = blockers
        #: True: terminal ``final`` degil; retry/resume ile devam edilebilir.
        self.recoverable = recoverable


@dataclass(frozen=True, slots=True)
class WorkBinding:
    """Mevcut Work Graph baglami; loop bunlari uydurmaz, cagiran verir."""

    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    coordinator_assignment_id: UUID
    plan_id: UUID | None = None
    step_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTask:
    specialty: AgentSpecialty
    request_digest: str
    ordinal: int
    sequence: int
    context: Mapping[str, Any]
    read_resources: tuple[str, ...] = ()
    write_resources: tuple[str, ...] = ()
    risk: str = "medium"


@dataclass(frozen=True, slots=True)
class AgentResponse:
    specialty: AgentSpecialty
    assignment_id: UUID
    invocation_id: UUID
    envelope: AgentResultEnvelope
    body: Mapping[str, Any]
    context_bytes: int
    output_bytes: int
    #: "canonical" uretim yolu; testlerde istemci replay/mock ise adapter etiketi.
    client_id: str

    @property
    def envelope_digest(self) -> str:
        return self.envelope.result_digest


class UnitTestAgentGateway(Protocol):
    @property
    def remote(self) -> bool: ...

    def invoke(self, task: AgentTask) -> AgentResponse: ...


def _deterministic(kind: str, task: AgentTask) -> UUID:
    return uuid5(
        _NAMESPACE,
        f"{kind}:{task.request_digest}:{task.ordinal}:{task.specialty.value}:{task.sequence}",
    )


@dataclass(frozen=True, slots=True)
class CanonicalUnitTestAgentGateway:
    """Gercek canonical dispatch yolu: assignment-first + istemci adapter'i."""

    work: WorkBinding
    store: AssignmentStore
    objects: ObjectStore
    adapters: Mapping[AgentSpecialty, ClientAdapter]
    cwd: Path
    timeout_seconds: int = 600
    max_context_bytes: int = 200_000
    is_remote: bool = False
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)

    @property
    def remote(self) -> bool:
        return self.is_remote

    def _load_artifact(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Sonuc govdesi/zarf alanlari ayri content-addressed artifact olabilir.

        Mevcut ``DispatchResult`` sanitizer'i her alanda "token/password/..." SOZCUGUNU
        secret sayar (PasswordValidator testi gibi mesru kodu reddeder). Sanitizer
        zayiflatilmaz: zarf yalniz artifact digest'ini tasir; icerik burada boyut siniri,
        digest dogrulamasi ve GERCEK secret degeri taramasindan gecer.
        """

        if set(payload) != {"artifact_digest"}:
            return payload
        digest_value = payload["artifact_digest"]
        try:
            info = self.objects.stat(str(digest_value))
            if info.size_bytes > MAX_ARTIFACT_BYTES:
                raise AgentDispatchFailure("artifact-too-large")
            raw = self.objects.get(str(digest_value))
        except (NotFound, ValidationFailed) as exc:
            raise AgentDispatchFailure("artifact-unavailable") from exc
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AgentDispatchFailure("artifact-unparsable") from exc
        if not isinstance(document, dict):
            raise AgentDispatchFailure("artifact-unparsable")
        if find_secret_value(document) is not None:
            raise AgentDispatchFailure("result-contains-secret-value")
        return document

    def _envelope(self, payload: Mapping[str, Any], task: AgentTask) -> AgentResultEnvelope:
        """Redakte sonuc -> strict envelope. Rol harness'tan gelir (model rol beyan edemez).

        Mevcut ``DispatchResult`` sanitizer'i ``token_count`` gibi alanlari secret sayar; bu
        yuzden tam envelope degil ``result`` ozeti tasinir ve envelope burada kurulur.
        """

        if set(payload) != {"result", "body"} or not isinstance(payload["body"], dict):
            raise AgentDispatchFailure("invalid-agent-payload")
        result = payload["result"]
        allowed = {"agent_id", "status", "summary", "evidence", "findings", "blockers"}
        if not isinstance(result, Mapping) or set(result) - allowed or "agent_id" not in result:
            raise AgentDispatchFailure("invalid-result-summary")
        try:
            evidence = tuple(
                EnvelopeEvidence(str(e["kind"]), str(e["reference"]), e.get("content_digest"))
                for e in result.get("evidence", [])
            )
            envelope = AgentResultEnvelope.create(
                agent_id=str(result["agent_id"]),
                role=AgentRole(ROLE_FOR[task.specialty].value),
                status=EnvelopeStatus(str(result.get("status", "completed"))),
                summary=str(result.get("summary", "")),
                evidence=evidence,
                findings=tuple(str(f) for f in result.get("findings", [])),
                blockers=tuple(str(b) for b in result.get("blockers", [])),
                now=self.now(),
            )
        except (ValidationFailed, ValueError, KeyError, TypeError) as exc:
            raise AgentDispatchFailure("invalid-envelope") from exc
        if envelope.status is not EnvelopeStatus.COMPLETED:
            raise AgentDispatchFailure(f"envelope-{envelope.status.value}", envelope.blockers)
        return envelope

    def invoke(self, task: AgentTask) -> AgentResponse:
        adapter = self.adapters.get(task.specialty)
        if adapter is None:
            raise AgentDispatchFailure("no-adapter-for-specialty")
        context_doc = {
            "contract": "zekam-unit-test-agent-context/v1",
            "specialty": task.specialty.value,
            "ordinal": task.ordinal,
            "context": task.context,
        }
        context_bytes = canonical_bytes(context_doc)
        if len(context_bytes) > self.max_context_bytes:
            raise PolicyViolation("Agent context siniri asildi; kesilerek cagri yapilmaz")
        if find_secret_value(context_doc) is not None:
            raise PolicyViolation("Agent context secret degeri tasiyamaz")
        instruction = (_INSTRUCTIONS[task.specialty] + _FORMAT_NOTE).encode("utf-8")
        instruction_digest = self.objects.put(instruction, media_type="text/plain").digest
        context_digest = self.objects.put(context_bytes, media_type="application/json").digest

        assignment_id = _deterministic("assignment", task)
        draft = AgentAssignment(
            id=assignment_id,
            realm_id=self.work.realm_id,
            project_id=self.work.project_id,
            work_item_id=self.work.work_item_id,
            role=ROLE_FOR[task.specialty],
            agent_ref=f"unit-test-{task.specialty.value}",
            instruction_digest=instruction_digest,
            context_manifest_digest=context_digest,
            assignment_digest=digest("draft"),
            parent_assignment_id=self.work.coordinator_assignment_id,
            plan_id=self.work.plan_id,
            step_id=self.work.step_id,
            risk=task.risk,
            read_resources=task.read_resources,
            write_resources=task.write_resources,
            created_at=self.now(),
        )
        assignment = replace(draft, assignment_digest=digest(draft.identity_body()))
        invocation_id = _deterministic("invocation", task)
        identity = f"{adapter.descriptor.client_id}:{task.specialty.value}:{task.ordinal}"
        body = {
            "id": str(invocation_id),
            "realm_id": str(self.work.realm_id),
            "assignment_id": str(assignment_id),
            "client_id": adapter.descriptor.client_id,
            "execution_identity": identity,
        }
        invocation = AgentInvocation(
            id=invocation_id,
            realm_id=self.work.realm_id,
            assignment_id=assignment_id,
            client_id=adapter.descriptor.client_id,
            execution_identity=identity,
            invocation_digest=digest(body),
            created_at=self.now(),
        )
        try:
            result = CanonicalAgentDispatchService(self.store).dispatch(
                assignment,
                invocation,
                adapter,
                cwd=self.cwd,
                timeout_seconds=self.timeout_seconds,
            )
        except PolicyViolation as exc:
            # DispatchResult secret-benzeri icerigi (ornegin "token" sozcugu) reddeder.
            raise AgentDispatchFailure(
                "result-rejected-by-dispatch-policy", recoverable=True
            ) from exc
        if not result.is_success:
            raise AgentDispatchFailure(result.failure_category or result.outcome.value)
        payload = self._load_artifact(result.payload)
        envelope = self._envelope(payload, task)
        return AgentResponse(
            specialty=task.specialty,
            assignment_id=assignment_id,
            invocation_id=invocation_id,
            envelope=envelope,
            body=payload["body"],
            context_bytes=len(context_bytes),
            output_bytes=len(canonical_bytes(payload)),
            client_id=adapter.descriptor.client_id,
        )


# -- role ozgu body dogrulama ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanBody:
    plan_document: Mapping[str, Any]
    context_gaps: tuple[str, ...]
    testability_findings: tuple[Mapping[str, Any], ...]


def parse_plan_body(body: Mapping[str, Any]) -> PlanBody:
    if (
        set(body) - {"analysis", "plan", "context_gaps", "testability_findings"}
        or "plan" not in body
    ):
        raise ValidationFailed("Planner body bilinmeyen/eksik alan")
    plan = body["plan"]
    if not isinstance(plan, Mapping):
        raise ValidationFailed("plan sozluk olmali")
    gaps = body.get("context_gaps", [])
    findings = body.get("testability_findings", [])
    if not isinstance(gaps, list) or not isinstance(findings, list):
        raise ValidationFailed("context_gaps/testability_findings liste olmali")
    for item in findings:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"observable", "tried_test_only", "evidence_refs"}
            or not item["evidence_refs"]
        ):
            raise ValidationFailed("Testability bulgusu kanit referansi ister")
    return PlanBody(plan, tuple(str(g)[:200] for g in gaps), tuple(findings))


@dataclass(frozen=True, slots=True)
class ScenarioLink:
    scenario_id: str
    test_ref: str


@dataclass(frozen=True, slots=True)
class BuildBody:
    patch: TestPatch
    links: tuple[ScenarioLink, ...]
    strategy: StrategyKind
    note: str
    plan_delta: PlanDelta | None
    characterization_updates: tuple[tuple[str, str], ...] = ()


def parse_build_body(body: Mapping[str, Any], request: UnitTestRequest) -> BuildBody:
    if set(body) - {"patch", "links", "approach", "plan_delta", "characterization_update"} or not {
        "patch",
        "links",
        "approach",
    } <= set(body):
        raise ValidationFailed("Builder body bilinmeyen/eksik alan")
    patch = parse_patch(body["patch"])
    raw_links = body["links"]
    if not isinstance(raw_links, list) or not raw_links:
        raise ValidationFailed("links test-case <-> senaryo iliskisi ister")
    links: list[ScenarioLink] = []
    for item in raw_links:
        if not isinstance(item, Mapping) or set(item) != {"scenario_id", "test_ref"}:
            raise ValidationFailed("link scenario_id/test_ref tasimali")
        links.append(ScenarioLink(str(item["scenario_id"]), str(item["test_ref"])[:200]))
    approach = body["approach"]
    if not isinstance(approach, Mapping) or set(approach) != {"strategy", "note"}:
        raise ValidationFailed("approach strategy/note tasimali")
    try:
        strategy = StrategyKind(str(approach["strategy"]))
    except ValueError as exc:
        raise ValidationFailed("Bilinmeyen strateji") from exc
    delta = body.get("plan_delta")
    updates: list[tuple[str, str]] = []
    for item in body.get("characterization_update", []):
        if not isinstance(item, Mapping) or set(item) != {"scenario_id", "evidence_ref"}:
            raise ValidationFailed("characterization_update scenario_id/evidence_ref tasimali")
        updates.append((str(item["scenario_id"]), str(item["evidence_ref"])[:200]))
    return BuildBody(
        patch,
        tuple(links),
        strategy,
        str(approach["note"])[:300],
        None if delta is None else parse_plan_delta(delta, request),
        tuple(updates),
    )


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REPLAN = "needs-replan"


#: Verifier'in bu kodlardan biriyle "accepted" demesi celiskidir; harness reddeder.
BLOCKING_QUALITY_CODES: Final = frozenset(
    {
        "tautology",
        "self-oracle",
        "weak-assertion",
        "private-reflection",
        "own-code-mock",
        "sleep-sync",
        "print-only",
        "scope-violation",
        "expected-copied-from-production",
    }
)


@dataclass(frozen=True, slots=True)
class DefectReview:
    scenario_id: str
    finding: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifyBody:
    verdict: Verdict
    verified: tuple[str, ...]
    unresolved: tuple[str, ...]
    quality_codes: tuple[str, ...]
    new_material_risks: tuple[BehaviorScenario, ...]
    defect_review: DefectReview | None
    fixture_ready: bool
    value_notes: tuple[str, ...]
    characterization_updates: tuple[str, ...] = ()

    @property
    def effective_verdict(self) -> Verdict:
        if self.verdict is Verdict.ACCEPTED and set(self.quality_codes) & BLOCKING_QUALITY_CODES:
            return Verdict.REJECTED
        return self.verdict


def parse_verify_body(body: Mapping[str, Any], request: UnitTestRequest) -> VerifyBody:
    allowed = {
        "verdict",
        "scenario_results",
        "quality_findings",
        "new_material_risks",
        "defect_review",
        "fixture_ready",
        "value_notes",
        "characterization_updates",
    }
    if set(body) - allowed or "verdict" not in body:
        raise ValidationFailed("Verifier body bilinmeyen/eksik alan")
    try:
        verdict = Verdict(str(body["verdict"]))
    except ValueError as exc:
        raise ValidationFailed("Gecersiz verdict") from exc
    verified: list[str] = []
    unresolved: list[str] = []
    for item in body.get("scenario_results", []):
        if not isinstance(item, Mapping) or set(item) - {"scenario_id", "status", "reason"}:
            raise ValidationFailed("scenario_results gecersiz")
        status = item.get("status")
        if status == "verified":
            verified.append(str(item["scenario_id"]))
        elif status == "unresolved":
            unresolved.append(str(item["scenario_id"]))
        else:
            # accepted-exception yalniz kullanici kaydidir; model veremez.
            raise PolicyViolation("Verifier yalniz verified/unresolved bildirebilir")
    codes = tuple(
        str(f.get("code", "")) for f in body.get("quality_findings", []) if isinstance(f, Mapping)
    )
    allowed_sources = frozenset(request.source_files)
    risks = tuple(
        parse_scenario(r, allowed_sources=allowed_sources)
        for r in body.get("new_material_risks", [])
    )
    review_doc = body.get("defect_review")
    review: DefectReview | None = None
    if review_doc is not None:
        if not isinstance(review_doc, Mapping) or set(review_doc) != {
            "scenario_id",
            "finding",
            "evidence_refs",
        }:
            raise ValidationFailed("defect_review gecersiz")
        if review_doc["finding"] not in {"defect-confirmed", "test-wrong", "inconclusive"}:
            raise ValidationFailed("defect_review.finding gecersiz")
        review = DefectReview(
            str(review_doc["scenario_id"]),
            str(review_doc["finding"]),
            tuple(str(e) for e in review_doc["evidence_refs"]),
        )
    return VerifyBody(
        verdict,
        tuple(verified),
        tuple(unresolved),
        codes,
        risks,
        review,
        bool(body.get("fixture_ready", False)),
        tuple(str(n)[:200] for n in body.get("value_notes", [])),
        tuple(str(c) for c in body.get("characterization_updates", [])),
    )
