"""Canonical project research run/status/report vertical slice."""

from __future__ import annotations

import datetime as dt
import json
import os
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
from zekam.application.markdown_knowledge import show_markdown_knowledge
from zekam.application.operational_store import OperationalStore
from zekam.application.project_rag_runtime import (
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
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.research import (
    Citation,
    CitationVerification,
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
from zekam.infrastructure.opencode_research import (
    OpenCodeResearchAdapter,
    OpenCodeResearchResult,
    require_opencode_execution,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

PLAN_SCHEMA = "zekam-research-run-plan/v1"
RUN_SCHEMA = "zekam-research-run-result/v1"
STATUS_SCHEMA = "zekam-research-run-status/v1"
REPORT_SURFACE_SCHEMA = "zekam-research-report-surface/v1"
_LOCAL_REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))
_REPORT_BEGIN = "<!-- zekam-research-report-json-begin -->"
_REPORT_END = "<!-- zekam-research-report-json-end -->"


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
    intent_digest = digest(
        {
            "operation": "research.run",
            "project_id": project.id,
            "question": question,
            "source_revision": rag["source_revision"],
        }
    )
    question_contract = ResearchQuestion(
        question_id=f"research-question:{intent_digest[7:]}",
        question=question,
        project_ref=project.slug,
        work_ref=f"project:{project.id}",
        intent_digest=intent_digest,
        source_revision=str(rag["source_revision"]),
        policy=SourcePolicy(
            frozenset({SourceKind.REPOSITORY}),
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
        "source_revision": rag["source_revision"],
        "generation_digest": rag["generation_digest"],
        "retrieval": {
            "maximum_remote_query_operations": 1,
            "maximum_evidence_chunks": 5,
            "row_data_allowed": False,
        },
        "agents": {
            "primary": "zekam-research-runner",
            "delegates": ["zekam-researcher", "zekam-verifier"],
            "maximum_model_agent_calls": 3,
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


def _bounded_evidence(
    home: Path, plan: ResearchRunPlan, rag: dict[str, Any]
) -> tuple[tuple[dict[str, Any], ...], tuple[SourceSnapshot, ...], dict[str, dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    snapshots: list[SourceSnapshot] = []
    by_id: dict[str, dict[str, Any]] = {}
    citations = rag.get("citations", [])
    if not isinstance(citations, list):
        raise ValidationFailed("Research RAG citations liste olmali")
    for citation in citations[:5]:
        if not isinstance(citation, dict):
            raise ValidationFailed("Research RAG citation object olmali")
        citation_id = str(citation.get("chunk_id", ""))
        reopened = read_project_citation(
            home,
            plan.project_slug,
            citation_id,
            generation_digest=str(plan.body["generation_digest"]),
        )
        body = str(reopened["body"])
        if scan_text(body, relative_path=str(reopened["source_ref"])):
            raise PolicyViolation("Research evidence secret taramasini gecemedi")
        body = body[:4000]
        item = {
            "citation_id": citation_id,
            "source_ref": reopened["source_ref"],
            "source_revision": reopened["source_revision"],
            "content_digest": reopened["content_digest"],
            "locator": reopened["locator"],
            "body": body,
        }
        evidence.append(item)
        by_id[citation_id] = item
        snapshots.append(
            SourceSnapshot(
                snapshot_id=citation_id,
                kind=SourceKind.REPOSITORY,
                locator=str(reopened["source_ref"]),
                content_digest=str(reopened["content_digest"]),
                captured_at=_now(),
                revision=str(reopened["source_revision"]),
            )
        )
    return tuple(evidence), tuple(snapshots), by_id


def _role_result(
    result: OpenCodeResearchResult, by_citation: dict[str, dict[str, Any]]
) -> tuple[RoleResult, CitationVerification]:
    findings: list[Finding] = []
    for item in result.findings:
        citations = tuple(
            Citation(
                snapshot_id=citation_id,
                locator_detail=canonical_json(by_citation[citation_id]["locator"]),
                content_digest=str(by_citation[citation_id]["content_digest"]),
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
        findings=tuple(findings),
        objections=result.objections,
        blocker=result.blocker,
    )
    verification = CitationVerification(
        verifier_ref=result.verifier_ref,
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
        "## Doğrulanmış bulgular",
        "",
    ]
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
) -> dict[str, Any]:
    """Execute one digest-bound run behind a durable effect claim."""

    if expected_run_digest != plan.run_digest:
        raise PolicyViolation("Research apply exact run digest ister")
    if not authorize_remote_query or not authorize_agent_run:
        raise PolicyViolation("Research apply exact remote-query ve agent-run authorization ister")
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    payload = dict(plan.body)
    payload["dry_run"] = False
    job, created = runtime.enqueue(
        idempotency_key=str(plan.body["idempotency_key"]),
        payload=payload,
        max_attempts=1,
    )
    if not created:
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
        )
        if rag.get("generation_digest") != plan.body["generation_digest"]:
            raise PolicyViolation("Research retrieval generation drift")
        evidence, snapshots, by_citation = _bounded_evidence(home, plan, rag)
        package = {
            "schema": "zekam-opencode-research-package/v1",
            "question": question.question,
            "question_digest": question.question_digest,
            "project_slug": plan.project_slug,
            "source_revision": plan.body["source_revision"],
            "evidence": list(evidence),
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
        selected_adapter = adapter or OpenCodeResearchAdapter(
            cwd=resolve_project_source(home, plan.project_slug)
        )
        agent_result = selected_adapter.execute(package)
        execution = require_opencode_execution(agent_result)
        execution_evidence = execution.as_dict()
        role_result, verification = _role_result(agent_result, by_citation)
        dispatch = DispatchReport(
            question_id=question.question_id,
            subagent_count=agent_result.delegated_agent_calls,
            coordinator_produced_results=False,
            groups=tuple((item.agent_type,) for item in execution.calls),
            results=(role_result,),
        )
        service = ResearchService()
        service.validate_question(
            question,
            current_source_revision=str(plan.body["source_revision"]),
            current_intent_digest=question.intent_digest,
            snapshots=snapshots,
        )
        report = service.build_report(
            question,
            dispatch,
            report_id=f"research-report:{plan.run_digest[7:]}",
            conflicts=(),
            verification=verification,
            snapshots=snapshots,
        )
        assert_no_swallowed_results(dispatch, report)
        report_document = report.as_dict()
        report_document.pop("report_digest")
        report_document["agent_execution"] = execution_evidence
        report_document["report_digest"] = digest(report_document)
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
    except Exception as exc:
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
    body: dict[str, Any] = {
        "schema": REPORT_SURFACE_SCHEMA,
        "job_id": status["job_id"],
        "run_digest": status["run_digest"],
        "state": status["state"],
        "projection_ref": status["projection_ref"],
        "projection_content_digest": note["content_digest"],
        "report": report,
        "verified": True,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}
