"""Canonical project research run/status/report vertical slice."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    generated_note_bytes,
    note_content_digest,
)
from zekam.application.local_runtime import LocalJobBinding, validate_dispatch_binding
from zekam.application.markdown_knowledge import show_markdown_knowledge
from zekam.application.operational_store import OperationalStore
from zekam.application.project_knowledge_index import source_identity_digest
from zekam.application.project_rag_runtime import (
    project_rag_source_freshness,
    project_rag_status,
    query_registered_project,
    read_project_citation,
    resolve_project_source,
)
from zekam.application.research_service import (
    DispatchReport,
    ResearchService,
    assert_no_swallowed_results,
)
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.research import (
    Citation,
    CitationVerification,
    Conflict,
    ConflictKind,
    Finding,
    ResearchBudget,
    ResearchQuestion,
    ResearchRole,
    RoleOutcome,
    RoleResult,
    SourceKind,
    SourcePolicy,
    SourceSnapshot,
)
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.native_research import (
    NativeResearchAdapter,
    NativeResearchHost,
    require_research_execution,
)
from zekam.infrastructure.opencode_research import (
    OpenCodeResearchAdapter,
    OpenCodeResearchResult,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

PLAN_SCHEMA = "zekam-research-run-plan/v1"
RUN_SCHEMA = "zekam-research-run-result/v1"
STATUS_SCHEMA = "zekam-research-run-status/v1"
REPORT_SURFACE_SCHEMA = "zekam-research-report-surface/v1"
_LOCAL_REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))
_REPORT_BEGIN = "<!-- zekam-research-report-json-begin -->"
_REPORT_END = "<!-- zekam-research-report-json-end -->"

# Evidence limits are derived from the question token budget.  The ceilings
# protect old or hand-crafted plans from becoming unbounded while the
# per-run budget determines the normal delivery size.
_EVIDENCE_CHARS_PER_TOKEN = 4
_EVIDENCE_ITEM_TOKEN_TARGET = 256
_MAX_EVIDENCE_ITEMS = 64
_MAX_EVIDENCE_CHARS = 128_000


class ResearchAdapter(Protocol):
    def execute(self, package: dict[str, Any]) -> OpenCodeResearchResult: ...


@dataclass(frozen=True, slots=True)
class ResearchRunPlan:
    body: dict[str, Any]

    @property
    def run_digest(self) -> str:
        return str(self.body["run_digest"])

    @property
    def project_id(self) -> str:
        return str(self.body["project_id"])

    @property
    def project_slug(self) -> str:
        return str(self.body["project_slug"])

    @property
    def projection_ref(self) -> str:
        return str(self.body["projection_ref"])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _timestamp(moment: dt.datetime | None = None) -> str:
    return (moment or _now()).isoformat().replace("+00:00", "Z")


def _research_source_binding(rag: dict[str, Any], *, project_id: str) -> dict[str, str]:
    """Read the exact RAG identity needed by a research plan.

    Older or hand-crafted status documents may omit the source identity
    fields.  Letting those documents reach direct mapping access produced a
    raw ``KeyError`` and obscured the bounded recovery action.  A research
    plan must carry a complete source binding, so reject the input with the
    domain policy error used by the other plan gates.
    """

    values: dict[str, str] = {}
    for key in ("source_revision", "tree_digest", "generation_digest"):
        value = rag.get(key)
        if type(value) is not str or not value.strip():
            raise PolicyViolation(f"Research RAG kaynak kimligi eksik: {key}")
        try:
            parse_digest(value)
        except ValidationFailed as exc:
            raise PolicyViolation(f"Research RAG kaynak kimligi gecersiz: {key}") from exc
        values[key] = value
    values["source_identity_digest"] = source_identity_digest(
        project_id, values["source_revision"], values["tree_digest"]
    )
    return values


def build_research_run_plan(
    store: OperationalStore,
    home: Path,
    *,
    project_ref: str,
    question: str,
) -> ResearchRunPlan:
    """Build a deterministic, provider-free plan for one indexed project."""

    with store.unit_of_work() as uow:
        project = uow.resolve_project(project_ref)
        uow.commit()
    rag = project_rag_status(home, project.slug)
    if rag.get("state") != "ready":
        raise PolicyViolation("Research run hazir project RAG generation ister")
    source_binding = _research_source_binding(rag, project_id=project.id)
    intent_digest = digest(
        {
            "operation": "research.run",
            "project_id": project.id,
            "question": question,
            "source_revision": source_binding["source_revision"],
        }
    )
    question_contract = ResearchQuestion(
        question_id=f"research-question:{intent_digest[7:]}",
        question=question,
        project_ref=project.slug,
        work_ref=f"project:{project.id}",
        intent_digest=intent_digest,
        source_revision=source_binding["source_revision"],
        policy=SourcePolicy(
            frozenset({SourceKind.REPOSITORY, SourceKind.IMPORT}),
            project_scope=project.slug,
            allow_row_data=False,
        ),
        budget=ResearchBudget(
            max_tokens=12_000,
            max_cost_units=3,
            max_seconds=600,
            max_rounds=1,
        ),
        created_at=_now(),
    )
    stable = {
        "schema": PLAN_SCHEMA,
        "operation": "research.run",
        "project_id": project.id,
        "project_slug": project.slug,
        "question": question_contract.body(),
        "question_digest": question_contract.question_digest,
        "source_revision": source_binding["source_revision"],
        "tree_digest": source_binding["tree_digest"],
        "source_identity_digest": source_binding["source_identity_digest"],
        "generation_digest": source_binding["generation_digest"],
        "retrieval": {
            "maximum_remote_query_operations": 1,
            # Retained for consumers of the v1 plan schema.  New runtimes use
            # the token-derived budget below instead of truncating at five.
            "maximum_evidence_chunks": 5,
            "evidence_budget": _derived_evidence_budget(question_contract),
            "row_data_allowed": False,
        },
        "agents": {
            "primary": "zekam-research-runner",
            "delegates": ["zekam-researcher", "zekam-verifier"],
            "maximum_model_agent_calls": 3,
            "logical_roles": ["researcher", "verifier"],
            "named_agents_required": False,
        },
        "requires_remote_query_authorization": True,
        "requires_agent_run_authorization": True,
        "provider_calls_performed": 0,
        "grants_authority": False,
    }
    run_digest = digest(stable)
    projection_ref = f"projeler/{project.slug}/arastirmalar/generated/{run_digest[7:]}.md"
    body = stable | {
        "run_digest": run_digest,
        "idempotency_key": f"research:{run_digest}",
        "projection_ref": projection_ref,
        "dry_run": True,
    }
    return ResearchRunPlan(body)


def _question_from_plan(plan: ResearchRunPlan) -> ResearchQuestion:
    body = plan.body["question"]
    assert isinstance(body, dict)
    policy = body["policy"]
    budget = body["budget"]
    return ResearchQuestion(
        question_id=str(body["question_id"]),
        question=str(body["question"]),
        project_ref=str(body["project_ref"]),
        work_ref=str(body["work_ref"]),
        intent_digest=str(body["intent_digest"]),
        source_revision=str(body["source_revision"]),
        policy=SourcePolicy(
            frozenset(SourceKind(item) for item in policy["allowed_kinds"]),
            allowed_hosts=frozenset(str(item) for item in policy["allowed_hosts"]),
            project_scope=policy["project_scope"],
            allow_row_data=bool(policy["allow_row_data"]),
        ),
        budget=ResearchBudget(
            max_tokens=int(budget["max_tokens"]),
            max_cost_units=int(budget["max_cost_units"]),
            max_seconds=int(budget["max_seconds"]),
            max_rounds=int(budget["max_rounds"]),
        ),
        created_at=_now(),
    )


def _derived_evidence_budget(question: ResearchQuestion) -> dict[str, int]:
    """Derive bounded evidence delivery from the research token budget."""

    return {
        "maximum_items": min(
            _MAX_EVIDENCE_ITEMS,
            max(1, question.budget.max_tokens // _EVIDENCE_ITEM_TOKEN_TARGET),
        ),
        "maximum_chars": min(
            _MAX_EVIDENCE_CHARS,
            max(_EVIDENCE_CHARS_PER_TOKEN, question.budget.max_tokens * _EVIDENCE_CHARS_PER_TOKEN),
        ),
    }


def _evidence_budget(plan: ResearchRunPlan) -> dict[str, int]:
    """Read an optional plan budget, retaining dynamic defaults for old plans."""

    question = _question_from_plan(plan)
    default = _derived_evidence_budget(question)
    retrieval = plan.body.get("retrieval", {})
    if not isinstance(retrieval, dict):
        raise ValidationFailed("Research retrieval plani object olmali")
    configured = retrieval.get("evidence_budget")
    if configured is None:
        return default
    if not isinstance(configured, dict):
        raise ValidationFailed("Research evidence budget object olmali")
    values: dict[str, int] = {}
    for key, ceiling in (
        ("maximum_items", _MAX_EVIDENCE_ITEMS),
        ("maximum_chars", _MAX_EVIDENCE_CHARS),
    ):
        value = configured.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationFailed(f"Research evidence budget {key} pozitif tamsayi olmali")
        if value > ceiling:
            raise PolicyViolation(f"Research evidence budget {key} bounded siniri asiyor")
        values[key] = value
    return values


def _bounded_evidence(
    home: Path, plan: ResearchRunPlan, rag: dict[str, Any]
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[SourceSnapshot, ...],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    evidence: list[dict[str, Any]] = []
    snapshots: list[SourceSnapshot] = []
    by_id: dict[str, dict[str, Any]] = {}
    citations = rag.get("citations", [])
    if not isinstance(citations, list):
        raise ValidationFailed("Research RAG citations liste olmali")
    budget = _evidence_budget(plan)
    allowed_kinds = _question_from_plan(plan).policy.allowed_kinds
    selected_chars = 0
    truncated_items = 0
    gaps: list[str] = []
    retrieval_evidence_sufficient = rag.get("evidence_sufficient")
    if retrieval_evidence_sufficient is False:
        gaps.append("retrieval-evidence-gap")
    index_freshness = rag.get("index_freshness", "unknown")
    if not isinstance(index_freshness, str) or index_freshness not in {
        "current",
        "stale",
        "unknown",
    }:
        raise ValidationFailed("Research RAG index_freshness degeri gecersiz")
    stale_reasons_value = rag.get("stale_reasons", [])
    if not isinstance(stale_reasons_value, list):
        raise ValidationFailed("Research RAG stale_reasons liste olmali")
    stale_reasons: list[str] = []
    for reason in stale_reasons_value:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationFailed("Research RAG stale_reasons bounded metin olmali")
        stale_reasons.append(reason)
    snapshot_only = rag.get("snapshot_only", False)
    if not isinstance(snapshot_only, bool):
        raise ValidationFailed("Research RAG snapshot_only boolean olmali")
    reindex_recommended = rag.get(
        "reindex_recommended", index_freshness == "stale" or bool(stale_reasons)
    )
    if not isinstance(reindex_recommended, bool):
        raise ValidationFailed("Research RAG reindex_recommended boolean olmali")
    if index_freshness == "unknown" and citations:
        gaps.append("retrieval-freshness-unavailable")
    if index_freshness == "stale" or stale_reasons or snapshot_only:
        gaps.append("retrieval-index-stale")
    for citation in citations:
        if len(evidence) >= budget["maximum_items"] or selected_chars >= budget["maximum_chars"]:
            gaps.append("evidence-budget-exhausted")
            break
        if not isinstance(citation, dict):
            raise ValidationFailed("Research RAG citation object olmali")
        citation_id = str(citation.get("chunk_id", ""))
        reopened = read_project_citation(
            home,
            plan.project_slug,
            citation_id,
            generation_digest=str(plan.body["generation_digest"]),
        )
        source_revision = str(reopened["source_revision"])
        if source_revision != str(plan.body["source_revision"]):
            raise PolicyViolation("Research evidence source revision drift")
        kind = (
            SourceKind.IMPORT
            if reopened.get("locator_type") == "project-knowledge-reference"
            else SourceKind.REPOSITORY
        )
        if kind not in allowed_kinds:
            gaps.append("reference-source-not-permitted")
            continue
        body = str(reopened["body"])
        if scan_text(body, relative_path=str(reopened["source_ref"])):
            raise PolicyViolation("Research evidence secret taramasini gecemedi")
        original_content_digest = str(reopened["content_digest"])
        remaining_chars = budget["maximum_chars"] - selected_chars
        delivered_body = body[:remaining_chars]
        selected_chars += len(delivered_body)
        if len(delivered_body) < len(body):
            truncated_items += 1
        delivered_payload_digest = digest(delivered_body)
        item = {
            "citation_id": citation_id,
            "source_kind": kind.value,
            "trust": reopened.get("trust", "source-evidence"),
            "source_ref": reopened["source_ref"],
            "source_revision": source_revision,
            "original_content_digest": original_content_digest,
            "delivered_payload_digest": delivered_payload_digest,
            "content_digest": reopened["content_digest"],
            "locator": reopened["locator"],
            "body": delivered_body,
        }
        evidence.append(item)
        by_id[citation_id] = item
        snapshots.append(
            SourceSnapshot(
                snapshot_id=citation_id,
                kind=kind,
                locator=str(reopened["source_ref"]),
                content_digest=str(reopened["content_digest"]),
                captured_at=_now(),
                revision=str(reopened["source_revision"]),
            )
        )
    if not citations:
        gaps.append("no-citations")
    if len(evidence) < len(citations) and "evidence-budget-exhausted" not in gaps:
        gaps.append("evidence-budget-exhausted")
    if truncated_items:
        gaps.append("evidence-body-truncated")
    if not evidence and gaps:
        state = "evidence-gap"
    elif gaps:
        state = "partial"
    else:
        state = "complete"
    coverage = {
        "schema": "zekam-research-evidence-coverage/v1",
        "state": state,
        "requested_items": len(citations),
        "selected_items": len(evidence),
        "omitted_items": len(citations) - len(evidence),
        "selected_chars": selected_chars,
        "truncated_items": truncated_items,
        "budget": dict(budget),
        "retrieval_evidence_sufficient": retrieval_evidence_sufficient,
        "index_freshness": index_freshness,
        "stale_reasons": list(dict.fromkeys(stale_reasons)),
        "snapshot_only": snapshot_only,
        "reindex_recommended": reindex_recommended,
        "uncovered_identifiers": list(rag.get("uncovered_identifiers", [])),
        "gaps": list(dict.fromkeys(gaps)),
    }
    return tuple(evidence), tuple(snapshots), by_id, coverage


def _objection_conflicts(result: OpenCodeResearchResult, role: RoleResult) -> tuple[Conflict, ...]:
    """Expose bounded adapter objections as unresolved evidence gaps."""

    if not result.objections:
        return ()
    finding_ids = tuple(item.finding_id for item in role.findings)
    left_id = finding_ids[0] if finding_ids else "research-objection-anchor"
    conflicts: list[Conflict] = []
    for objection in result.objections:
        if not isinstance(objection, str) or not objection.strip() or len(objection) > 1000:
            raise ValidationFailed("Research objection bounded metin olmali")
        if scan_text(objection, relative_path="opencode-research-objection"):
            raise PolicyViolation("Research objection secret taramasini gecemedi")
        objection_digest = digest(objection)[7:]
        right_id = f"objection:{objection_digest}"
        if right_id == left_id:
            right_id = f"objection:{objection_digest}:right"
        conflicts.append(
            Conflict(
                conflict_id=f"research-objection:{objection_digest}",
                kind=ConflictKind.EVIDENCE_GAP,
                left_finding_id=left_id,
                right_finding_id=right_id,
                detail=objection,
            )
        )
    return tuple(conflicts)


def _execution_usage(execution: Any) -> list[dict[str, Any]]:
    """Report JSON-array model usage while keeping unknowns explicit."""

    by_model: dict[str, dict[str, Any]] = {}
    for call in execution.calls:
        model_ref = f"{call.provider_id}/{call.model_id}"
        bucket = by_model.setdefault(
            model_ref,
            {
                "model_ref": model_ref,
                "agent_calls": 0,
                "provider_calls": None,
                "tokens": None,
                "duration_seconds": None,
                "cost_micros": None,
                "disk_bytes": None,
                "metrics_state": "unknown",
                "measurement_reason": "Host execution event usage alani tasimiyor",
            },
        )
        bucket["agent_calls"] += 1
    # Research reports are persisted as canonical JSON.  Return the same list
    # shape exposed by that wire contract instead of an in-memory tuple that
    # silently becomes a list during materialization.
    return list(by_model.values())


def _role_result(
    result: OpenCodeResearchResult, by_citation: dict[str, dict[str, Any]]
) -> tuple[RoleResult, CitationVerification]:
    findings: list[Finding] = []
    for item in result.findings:
        citations = tuple(
            Citation(
                snapshot_id=citation_id,
                locator_detail=canonical_json(by_citation[citation_id]["locator"]),
                content_digest=str(by_citation[citation_id]["delivered_payload_digest"]),
                source_content_digest=str(by_citation[citation_id]["original_content_digest"]),
                slice_digest=str(by_citation[citation_id]["delivered_payload_digest"]),
            )
            for citation_id in item["citation_ids"]
        )
        findings.append(
            Finding(
                finding_id=str(item["finding_id"]),
                claim=str(item["claim"]),
                citations=citations,
                confidence=str(item["confidence"]),
            )
        )
    role = RoleResult(
        role=ResearchRole.RESEARCHER,
        agent_ref=result.researcher_ref,
        outcome=RoleOutcome(result.outcome),
        payload_digest=result.researcher_payload_digest,
        findings=tuple(findings),
        objections=result.objections,
        blocker=result.blocker,
    )
    verification = CitationVerification(
        verifier_ref=result.verifier_ref,
        researcher_payload_digest=result.researcher_payload_digest,
        evidence_manifest_digest=result.evidence_manifest_digest,
        verified_finding_ids=result.verified_finding_ids,
        rejected_finding_ids=result.rejected_finding_ids,
        rejection_reasons=result.rejection_reasons,
    )
    return role, verification


def _report_markdown(question: str, report: dict[str, Any]) -> str:
    findings = report.get("findings", [])
    lines = [
        "# Araştırma raporu",
        "",
        f"**Soru:** {question}",
        f"**Durum:** {report['status']}",
        "",
    ]
    coverage = report.get("evidence_coverage")
    if isinstance(coverage, dict):
        lines.extend(
            [
                "## Kanıt kapsamı",
                "",
                f"- Durum: {coverage.get('state', 'unknown')}",
                (
                    f"- Seçilen kanıt: {coverage.get('selected_items', 0)} / "
                    f"{coverage.get('requested_items', 0)}"
                ),
                (
                    f"- Açıklar: "
                    f"{', '.join(str(item) for item in coverage.get('gaps', [])) or 'yok'}"
                ),
                "",
            ]
        )
    lines.extend(["## Doğrulanmış bulgular", ""])
    if findings:
        for item in findings:
            lines.append(f"- {item['claim']} (`{item['confidence']}`)")
    else:
        lines.append("Kullanılabilir doğrulanmış bulgu yok; sistem abstain etti.")
    lines.extend(
        [
            "",
            "## Kanonik JSON",
            "",
            _REPORT_BEGIN,
            "```json",
            canonical_json(report),
            "```",
            _REPORT_END,
            "",
        ]
    )
    return "\n".join(lines)


def _materialize_report(
    store: OperationalStore,
    files: KnowledgeFileStore,
    plan: ResearchRunPlan,
    report: dict[str, Any],
    *,
    job_id: str,
) -> dict[str, Any]:
    question = str(plan.body["question"]["question"])
    report_digest = str(report["report_digest"])
    payload = generated_note_bytes(
        owner_scope=f"project:{plan.project_id}",
        project_slug=plan.project_slug,
        note_kind="research",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=(f"research-runs/{job_id}",),
        source_digests=(report_digest,),
        generated_at=_timestamp(),
        generator_version="zekam-research-runtime/v1",
        body=_report_markdown(question, report),
    )
    if scan_text(payload.decode("utf-8"), relative_path=plan.projection_ref):
        raise PolicyViolation("Research report secret taramasini gecemedi")
    content_digest = note_content_digest(payload)
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{plan.project_id}",
        project_slug=plan.project_slug,
        note_kind="research",
        authorship="generated",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref=plan.projection_ref,
        content_digest=content_digest,
    )
    with store.unit_of_work() as uow:
        note = uow.register_knowledge_note(
            realm_id=_LOCAL_REALM_ID,
            project_id=plan.project_id,
            owner_scope=manifest.owner_scope,
            portable_ref=manifest.portable_ref,
            note_kind=manifest.note_kind,
            authorship=manifest.authorship,
            classification=manifest.classification.value,
            content_digest=manifest.content_digest,
        )
        files.create_note(manifest, payload)
        evidence_digest = digest(
            {
                "portable_ref": manifest.portable_ref,
                "content_digest": content_digest,
                "report_digest": report_digest,
            }
        )
        confirmed = uow.confirm_knowledge_note(
            note_id=note.id,
            expected_content_digest=content_digest,
            evidence_digest=evidence_digest,
        )
        uow.commit()
    return {
        "note_id": confirmed.id,
        "portable_ref": confirmed.portable_ref,
        "content_digest": confirmed.content_digest,
        "materialization_evidence_digest": evidence_digest,
    }


def run_research(
    store: OperationalStore,
    home: Path,
    plan: ResearchRunPlan,
    *,
    expected_run_digest: str,
    authorize_remote_query: bool,
    authorize_agent_run: bool,
    opencode_config: Path | None = None,
    adapter: ResearchAdapter | None = None,
    work_binding: LocalJobBinding | None = None,
    cancel: threading.Event | None = None,
    native_host: NativeResearchHost | None = None,
) -> dict[str, Any]:
    """Execute one digest-bound run behind a durable effect claim."""

    if expected_run_digest != plan.run_digest:
        raise PolicyViolation("Research apply exact run digest ister")
    if native_host is not None and adapter is not None:
        raise PolicyViolation("Research tek adapter veya native host ister")
    if not authorize_remote_query or not authorize_agent_run:
        raise PolicyViolation("Research apply exact remote-query ve agent-run authorization ister")
    if cancel is not None and cancel.is_set():
        raise PolicyViolation("Research iptal edildi")
    required_source_binding = (
        "source_revision",
        "tree_digest",
        "source_identity_digest",
        "generation_digest",
    )
    if any(key not in plan.body for key in required_source_binding):
        raise PolicyViolation("Research plan source identity binding eksik")
    if work_binding is not None and (
        type(work_binding) is not LocalJobBinding
        or work_binding.project_id != plan.project_id
        or work_binding.plan_digest != plan.run_digest
    ):
        raise PolicyViolation("Research canonical Work/Run project-plan binding drift")
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    validate_dispatch_binding(
        runtime, work_binding, idempotency_key=str(plan.body["idempotency_key"])
    )
    payload = dict(plan.body)
    payload["dry_run"] = False
    job, created = runtime.enqueue(
        idempotency_key=str(plan.body["idempotency_key"]),
        payload=payload,
        max_attempts=1,
        **({"binding": work_binding} if work_binding is not None else {}),
    )
    # A durable, never-leased ready job can be resumed after interruption
    # between enqueue and claim. All other states take the existing replay /
    # recovery path; a prior effect is never retried by this branch.
    if not created and (job.state != "ready" or job.attempt_count != 0):
        status = research_status(runtime, job.id)
        status["replayed"] = True
        return status
    owner_token = os.urandom(32).hex()
    work = runtime.claim_next(
        owner_id=f"research-{os.getpid()}",
        owner_pid=os.getpid(),
        owner_token=owner_token,
        lease_seconds=600,
        resources=(f"research:{plan.run_digest}",),
        supported_operations=("research.run",),
        job_id=job.id,
    )
    if work is None:
        raise PolicyViolation("Research job claim edilemedi")
    effect = {
        "operation": "opencode.research",
        "run_digest": plan.run_digest,
        "project_id": plan.project_id,
        "source_revision": plan.body["source_revision"],
        "tree_digest": plan.body["tree_digest"],
        "source_identity_digest": plan.body["source_identity_digest"],
        "generation_digest": plan.body["generation_digest"],
        "maximum_remote_query_operations": 1,
        "maximum_model_agent_calls": 3,
    }
    claim, claim_created = runtime.claim_effect(
        work,
        operation="opencode.research",
        effect_digest=digest(effect),
        idempotency_key=f"research:{plan.run_digest}:effect",
    )
    if not claim_created:
        runtime.finish(work, state="recovery-required")
        raise PolicyViolation("Research effect claim replay; silent redispatch yasak")
    try:
        question = _question_from_plan(plan)
        rag = query_registered_project(
            home,
            plan.project_slug,
            question.question,
            opencode_config=opencode_config,
            authorize_remote_query=authorize_remote_query,
        )
        if rag.get("generation_digest") != plan.body["generation_digest"]:
            raise PolicyViolation("Research retrieval generation drift")
        evidence, snapshots, by_citation, evidence_coverage = _bounded_evidence(home, plan, rag)
        post_citation_freshness = project_rag_source_freshness(home, plan.project_slug)
        post_citation_reasons = tuple(
            str(reason)
            for reason in post_citation_freshness.get("stale_reasons", ())
            if isinstance(reason, str) and reason.strip()
        )
        if post_citation_freshness.get("state") != "current":
            gaps = list(evidence_coverage.get("gaps", ()))
            gaps.append("retrieval-source-drift-after-citation")
            gaps.extend(post_citation_reasons)
            evidence_coverage = evidence_coverage | {
                "state": "evidence-gap" if not evidence else "partial",
                "index_freshness": "stale",
                "stale_reasons": list(
                    dict.fromkeys(
                        [
                            *evidence_coverage.get("stale_reasons", ()),
                            *post_citation_reasons,
                        ]
                    )
                ),
                "reindex_recommended": True,
                "gaps": list(dict.fromkeys(gaps)),
                "post_citation_freshness": {
                    "state": post_citation_freshness.get("state"),
                    "stale_reasons": list(post_citation_reasons),
                },
            }
        else:
            evidence_coverage = evidence_coverage | {
                "post_citation_freshness": {
                    "state": "current",
                    "stale_reasons": [],
                }
            }
        live_rag = project_rag_status(home, plan.project_slug)
        if live_rag.get("state") != "ready":
            raise PolicyViolation("Research live project RAG hazir degil")
        live_source_revision = live_rag.get("source_revision")
        if not isinstance(live_source_revision, str) or not live_source_revision.strip():
            raise PolicyViolation("Research live source revision eksik")
        live_tree_digest = live_rag.get("tree_digest")
        if not isinstance(live_tree_digest, str) or not live_tree_digest.strip():
            raise PolicyViolation("Research live tree digest eksik")
        if plan.body.get("tree_digest") != live_tree_digest:
            raise PolicyViolation("Research live tree drift")
        if plan.body.get("source_identity_digest") != source_identity_digest(
            plan.project_id, live_source_revision, live_tree_digest
        ):
            raise PolicyViolation("Research live source identity drift")
        if live_rag.get("generation_digest") != plan.body["generation_digest"]:
            raise PolicyViolation("Research live retrieval generation drift")
        live_intent_digest = digest(
            {
                "operation": "research.run",
                "project_id": plan.project_id,
                "question": question.question,
                "source_revision": live_source_revision,
            }
        )
        service = ResearchService()
        service.validate_question(
            question,
            current_source_revision=live_source_revision,
            current_intent_digest=live_intent_digest,
            snapshots=snapshots,
        )
        package = {
            "schema": "zekam-opencode-research-package/v1",
            "question": question.question,
            "question_digest": question.question_digest,
            "project_slug": plan.project_slug,
            "source_revision": plan.body["source_revision"],
            "evidence": list(evidence),
            "budget": question.budget.as_dict(),
            "scope": {"project_id": plan.project_id, "run_digest": plan.run_digest},
            "output_contract": {
                "schema": "zekam-opencode-research-result/v1",
                "exact_top_level_keys": [
                    "schema",
                    "question_digest",
                    "researcher",
                    "verification",
                    "grants_authority",
                ],
                "finding_keys": ["finding_id", "claim", "confidence", "citation_ids"],
                "researcher_keys": [
                    "agent_ref",
                    "outcome",
                    "findings",
                    "objections",
                    "blocker",
                ],
                "verification_keys": [
                    "verifier_ref",
                    "researcher_payload_digest",
                    "evidence_manifest_digest",
                    "verified_finding_ids",
                    "rejected_finding_ids",
                    "rejection_reasons",
                ],
                "verifier_must_decide_every_finding": True,
                "citation_ids_must_come_from_evidence": True,
                "empty_collections_are_json_arrays": True,
                "example": {
                    "schema": "zekam-opencode-research-result/v1",
                    "question_digest": question.question_digest,
                    "researcher": {
                        "agent_ref": "zekam-researcher:<session>",
                        "outcome": "success",
                        "findings": [
                            {
                                "finding_id": "f1",
                                "claim": "Evidence-backed claim.",
                                "confidence": "high",
                                "citation_ids": ["exact-evidence-citation-id"],
                            }
                        ],
                        "objections": [],
                        "blocker": None,
                    },
                    "verification": {
                        "verifier_ref": "zekam-verifier:<session>",
                        "verified_finding_ids": ["f1"],
                        "rejected_finding_ids": [],
                        "rejection_reasons": [],
                    },
                    "grants_authority": False,
                },
                "grants_authority": False,
            },
            "grants_authority": False,
        }
        selected_adapter = adapter or (
            NativeResearchAdapter(native_host, cancel=cancel)
            if native_host is not None
            else OpenCodeResearchAdapter(
                cwd=resolve_project_source(home, plan.project_slug),
                cancel=cancel,
            )
        )
        if cancel is not None and cancel.is_set():
            raise PolicyViolation("Research iptal edildi")
        agent_result = selected_adapter.execute(package)
        if cancel is not None and cancel.is_set():
            raise PolicyViolation("Research iptal edildi; gec sonuc yayinlanamaz")
        execution = require_research_execution(agent_result)
        execution_evidence = execution.as_dict()
        role_result, verification = _role_result(agent_result, by_citation)
        conflicts = _objection_conflicts(agent_result, role_result)
        evidence_manifest_digest = digest(evidence)
        dispatch = DispatchReport(
            question_id=question.question_id,
            subagent_count=agent_result.delegated_agent_calls,
            coordinator_produced_results=False,
            groups=tuple((item.agent_type,) for item in execution.calls),
            results=(role_result,),
        )
        report = service.build_report(
            question,
            dispatch,
            report_id=f"research-report:{plan.run_digest[7:]}",
            conflicts=conflicts,
            verification=verification,
            snapshots=snapshots,
            evidence_manifest_digest=evidence_manifest_digest,
        )
        assert_no_swallowed_results(dispatch, report)
        report_document = report.as_dict()
        report_document.pop("report_digest")
        report_document["source_revision"] = plan.body["source_revision"]
        report_document["tree_digest"] = plan.body["tree_digest"]
        report_document["source_identity_digest"] = plan.body["source_identity_digest"]
        report_document["evidence_coverage"] = evidence_coverage
        if evidence_coverage["state"] != "complete" and report_document["status"] == "answered":
            report_document["status"] = "partial"
        report_document["agent_execution"] = execution_evidence | {
            "usage_by_model": _execution_usage(execution),
        }
        report_document["report_digest"] = digest(report_document)
        if cancel is not None and cancel.is_set():
            raise PolicyViolation("Research iptal edildi; rapor yayinlanamaz")
        projection = _materialize_report(
            store,
            KnowledgeFileStore(home),
            plan,
            report_document,
            job_id=job.id,
        )
        receipt = runtime.record_receipt(
            claim,
            status="completed",
            evidence_digest=str(report_document["report_digest"]),
        )
        runtime.finish(
            work,
            state="completed",
            evidence_digest=str(report_document["report_digest"]),
        )
    except BaseException as exc:
        failure_evidence = digest(
            {
                "operation": "opencode.research",
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        try:
            runtime.record_receipt(claim, status="failed", evidence_digest=failure_evidence)
            runtime.finish(work, state="failed", evidence_digest=failure_evidence)
        except ZekamError:
            pass
        raise
    body: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "job_id": job.id,
        "run_digest": plan.run_digest,
        "state": "completed",
        "report": report_document,
        "projection": projection,
        "receipt": {
            "claim_id": claim.id,
            "receipt_id": receipt.id,
            "status": receipt.status,
            "evidence_digest": receipt.evidence_digest,
        },
        "remote_query_operations": 1,
        "root_agent_calls": agent_result.root_agent_calls,
        "delegated_agent_calls": agent_result.delegated_agent_calls,
        "evidence_coverage": report_document["evidence_coverage"],
        "replayed": False,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}


def research_status(runtime: SQLiteLocalRuntimeStore, reference: str) -> dict[str, Any]:
    snapshot = runtime.job_snapshot(reference)
    if snapshot is None:
        raise NotFound("Research run bulunamadi")
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or payload.get("operation") != "research.run":
        raise ValidationFailed("Job reference research run degil")
    effects = snapshot["effects"]
    missing_receipt = any(item["receipt_id"] is None for item in effects)
    state = str(snapshot["state"])
    if missing_receipt and state not in {"running", "recovery-required"}:
        state = "recovery-required"
    body: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "job_id": snapshot["job_id"],
        "run_digest": payload["run_digest"],
        "project_id": payload["project_id"],
        "project_slug": payload["project_slug"],
        "state": state,
        "attempt_count": snapshot["attempt_count"],
        "projection_ref": payload["projection_ref"],
        "report_ready": state == "completed" and bool(effects),
        "effects": effects,
        "read_only": True,
        "grants_authority": False,
    }
    if "work_binding" in snapshot:
        body["work_binding"] = snapshot["work_binding"]
    if all(
        key in payload
        for key in (
            "source_revision",
            "tree_digest",
            "source_identity_digest",
            "generation_digest",
        )
    ):
        body.update(
            {
                "source_revision": payload["source_revision"],
                "tree_digest": payload["tree_digest"],
                "source_identity_digest": payload["source_identity_digest"],
                "generation_digest": payload["generation_digest"],
            }
        )
    return body | {"result_digest": digest(body)}


def _extract_report(markdown: str) -> dict[str, Any]:
    start = markdown.find(_REPORT_BEGIN)
    end = markdown.find(_REPORT_END)
    if start < 0 or end <= start:
        raise PolicyViolation("Research Markdown canonical JSON blogu eksik")
    block = markdown[start + len(_REPORT_BEGIN) : end].strip()
    if not block.startswith("```json\n") or not block.endswith("\n```"):
        raise PolicyViolation("Research Markdown canonical JSON fence drift")
    try:
        parsed = json.loads(block[len("```json\n") : -len("\n```")])
    except json.JSONDecodeError as exc:
        raise PolicyViolation("Research Markdown canonical JSON bozuk") from exc
    if not isinstance(parsed, dict) or parsed.get("schema") != "zekam-research-report/v1":
        raise PolicyViolation("Research Markdown report schema drift")
    report_digest = parsed.pop("report_digest", None)
    if report_digest != digest(parsed):
        raise PolicyViolation("Research Markdown report digest drift")
    parsed["report_digest"] = report_digest
    return parsed


def research_report(
    runtime: SQLiteLocalRuntimeStore,
    store: OperationalStore,
    files: KnowledgeFileStore,
    reference: str,
) -> dict[str, Any]:
    status = research_status(runtime, reference)
    if status["state"] != "completed":
        raise PolicyViolation("Research report terminal completed run ister")
    with store.unit_of_work() as uow:
        note = show_markdown_knowledge(
            uow,
            files,
            str(status["projection_ref"]),
            project_id=str(status["project_id"]),
            owner_scope=f"project:{status['project_id']}",
        )
        uow.commit()
    report = _extract_report(str(note["body"]))
    effects = status["effects"]
    receipt_evidence = effects[-1]["evidence_digest"] if effects else None
    if receipt_evidence != report["report_digest"]:
        raise PolicyViolation("Research receipt/report digest drift")
    execution = report.get("agent_execution")
    verified = isinstance(execution, dict) and int(execution.get("delegated_agent_calls", 0)) >= 2
    provenance = "verified" if verified else "legacy-unverified"
    body: dict[str, Any] = {
        "schema": REPORT_SURFACE_SCHEMA,
        "job_id": status["job_id"],
        "run_digest": status["run_digest"],
        "state": status["state"],
        "projection_ref": status["projection_ref"],
        "projection_content_digest": note["content_digest"],
        "report": report,
        "verified": verified,
        "provenance": provenance,
        "read_only": True,
        "grants_authority": False,
    }
    if all(
        key in status
        for key in (
            "source_revision",
            "tree_digest",
            "source_identity_digest",
            "generation_digest",
        )
    ):
        body.update(
            {
                "source_revision": status["source_revision"],
                "tree_digest": status["tree_digest"],
                "source_identity_digest": status["source_identity_digest"],
                "generation_digest": status["generation_digest"],
            }
        )
    return body | {"result_digest": digest(body)}
