from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zekam.application import research_runtime as subject
from zekam.application.home import HomeLayout
from zekam.application.research_runtime import (
    build_research_run_plan,
    research_report,
    research_status,
    run_research,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.research import Citation, Finding
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.opencode_research import (
    OpenCodeAgentCall,
    OpenCodeExecutionEvidence,
    OpenCodeResearchResult,
    bind_opencode_result_document,
    parse_opencode_research_events,
    validate_opencode_research_result,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit


class FakeAdapter:
    def execute(self, package):  # type: ignore[no-untyped-def]
        citation_id = package["evidence"][0]["citation_id"]
        researcher = {
            "agent_ref": "zekam-researcher:run-1",
            "outcome": "success",
            "findings": [
                {
                    "finding_id": "finding-1",
                    "claim": "Musteri servisi kaynakta tanimlidir.",
                    "confidence": "high",
                    "citation_ids": [citation_id],
                },
            ],
            "objections": [],
            "blocker": None,
        }
        evidence_manifest_digest = digest(package.get("evidence", []))
        return OpenCodeResearchResult(
            document={},
            researcher_ref="zekam-researcher:run-1",
            verifier_ref="zekam-verifier:run-2",
            outcome="success",
            findings=tuple(researcher["findings"]),
            objections=(),
            blocker=None,
            verified_finding_ids=("finding-1",),
            rejected_finding_ids=(),
            rejection_reasons=(),
            researcher_payload_digest=digest(researcher),
            evidence_manifest_digest=evidence_manifest_digest,
            execution=OpenCodeExecutionEvidence(
                root_session_id="root-1",
                calls=(
                    OpenCodeAgentCall(
                        call_id="call-1",
                        agent_type="zekam-researcher",
                        parent_session_id="root-1",
                        session_id="run-1",
                        provider_id="test",
                        model_id="test-model",
                        input_digest=digest("researcher-input"),
                        output_digest=digest("researcher-output"),
                    ),
                    OpenCodeAgentCall(
                        call_id="call-2",
                        agent_type="zekam-verifier",
                        parent_session_id="root-1",
                        session_id="run-2",
                        provider_id="test",
                        model_id="test-model",
                        input_digest=digest("verifier-input"),
                        output_digest=digest("verifier-output"),
                    ),
                ),
            ),
        )


def _runtime(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("demo")
    home = layout.root
    database = home / "state" / "operational.db"
    bootstrap(database)
    SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.commit()
    generation = digest("generation")
    revision = digest("revision")
    monkeypatch.setattr(
        subject,
        "project_rag_status",
        lambda *_: {
            "state": "ready",
            "index_readable": True,
            "provider_readiness": "unknown",
            "query_ready": False,
            "generation_digest": generation,
            "source_revision": revision,
        },
    )
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": generation,
            "citations": [{"chunk_id": "chunk-1"}],
        },
    )
    monkeypatch.setattr(
        subject,
        "read_project_citation",
        lambda *_args, **_kwargs: {
            "source_ref": "src/main/Demo.java",
            "source_revision": revision,
            "content_digest": digest("class Demo"),
            "locator": {"relative_path": "src/main/Demo.java", "start_line": 1},
            "body": "class DemoMusteriService {}",
        },
    )
    return home, store, project


def test_research_run_status_report_and_replay(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    home, store, project = _runtime(tmp_path, monkeypatch)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    result = run_research(
        store,
        home,
        plan,
        expected_run_digest=plan.run_digest,
        authorize_remote_query=True,
        authorize_agent_run=True,
        adapter=FakeAdapter(),
    )
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    status = research_status(runtime, result["job_id"])
    report = research_report(runtime, store, KnowledgeFileStore(home), result["job_id"])
    replay = run_research(
        store,
        home,
        plan,
        expected_run_digest=plan.run_digest,
        authorize_remote_query=True,
        authorize_agent_run=True,
        adapter=FakeAdapter(),
    )

    assert status["state"] == "completed"
    assert report["verified"] is True
    assert report["report"]["status"] == "answered"
    assert report["report"]["findings"][0]["finding_id"] == "finding-1"
    assert report["report"]["agent_execution"]["delegated_agent_calls"] == 2
    assert result["receipt"]["evidence_digest"] == report["report"]["report_digest"]
    assert replay["replayed"] is True
    assert replay["state"] == "completed"
    assert len(status["effects"]) == 1


def test_research_forwards_exact_remote_query_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    def query(*_args, **kwargs):  # type: ignore[no-untyped-def]
        observed.update(kwargs)
        return {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": [{"chunk_id": "chunk-1"}],
        }

    monkeypatch.setattr(subject, "query_registered_project", query)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    run_research(
        store,
        home,
        plan,
        expected_run_digest=plan.run_digest,
        authorize_remote_query=True,
        authorize_agent_run=True,
        adapter=FakeAdapter(),
    )

    assert observed["authorize_remote_query"] is True


def test_opencode_result_rejects_unknown_citation_and_non_independent_verifier() -> None:
    document = {
        "schema": "zekam-opencode-research-result/v1",
        "question_digest": digest("question"),
        "researcher": {
            "agent_ref": "agent-a",
            "outcome": "success",
            "findings": [
                {
                    "finding_id": "finding-1",
                    "claim": "Kaynakta servis tanimlidir.",
                    "confidence": "high",
                    "citation_ids": ["unknown"],
                }
            ],
            "objections": [],
            "blocker": None,
        },
        "verification": {
            "verifier_ref": "agent-b",
            "researcher_payload_digest": digest("placeholder"),
            "evidence_manifest_digest": digest("placeholder"),
            "verified_finding_ids": ["finding-1"],
            "rejected_finding_ids": [],
            "rejection_reasons": [],
        },
        "grants_authority": False,
    }
    evidence_manifest_digest = digest([])
    document["verification"]["researcher_payload_digest"] = digest(document["researcher"])
    document["verification"]["evidence_manifest_digest"] = evidence_manifest_digest
    with pytest.raises(PolicyViolation, match="known citation"):
        validate_opencode_research_result(
            document,
            question_digest=digest("question"),
            allowed_citation_ids=frozenset({"chunk-1"}),
            evidence_manifest_digest=evidence_manifest_digest,
        )

    document["researcher"]["findings"][0]["citation_ids"] = ["chunk-1"]
    document["verification"]["verifier_ref"] = "agent-a"
    document["verification"]["researcher_payload_digest"] = digest(document["researcher"])
    with pytest.raises(PolicyViolation, match="bagimsiz"):
        validate_opencode_research_result(
            document,
            question_digest=digest("question"),
            allowed_citation_ids=frozenset({"chunk-1"}),
            evidence_manifest_digest=evidence_manifest_digest,
        )


def test_opencode_result_requires_terminal_verdict_for_every_finding() -> None:
    document = {
        "schema": "zekam-opencode-research-result/v1",
        "question_digest": digest("question"),
        "researcher": {
            "agent_ref": "agent-a",
            "outcome": "success",
            "findings": [
                {
                    "finding_id": "finding-1",
                    "claim": "Kaynakta servis tanimlidir.",
                    "confidence": "high",
                    "citation_ids": ["chunk-1"],
                }
            ],
            "objections": [],
            "blocker": None,
        },
        "verification": {
            "verifier_ref": "agent-b",
            "researcher_payload_digest": digest(
                {
                    "agent_ref": "agent-a",
                    "outcome": "success",
                    "findings": [
                        {
                            "finding_id": "finding-1",
                            "claim": "Kaynakta servis tanimlidir.",
                            "confidence": "high",
                            "citation_ids": ["chunk-1"],
                        }
                    ],
                    "objections": [],
                    "blocker": None,
                }
            ),
            "evidence_manifest_digest": digest([]),
            "verified_finding_ids": [],
            "rejected_finding_ids": [],
            "rejection_reasons": [],
        },
        "grants_authority": False,
    }
    with pytest.raises(ValidationFailed, match="terminal karar"):
        validate_opencode_research_result(
            document,
            question_digest=digest("question"),
            allowed_citation_ids=frozenset({"chunk-1"}),
            evidence_manifest_digest=digest([]),
        )


def _task_event(agent_type: str, child_session: str, call_id: str) -> str:
    return json.dumps(
        {
            "type": "tool_use",
            "sessionID": "root-session",
            "part": {
                "type": "tool",
                "tool": "task",
                "callID": call_id,
                "state": {
                    "status": "completed",
                    "input": {"subagent_type": agent_type, "prompt": "bounded"},
                    "output": f'<task id="{child_session}" state="completed">ok</task>',
                    "metadata": {
                        "parentSessionId": "root-session",
                        "sessionId": child_session,
                        "model": {"providerID": "test", "modelID": "test-model"},
                        "truncated": False,
                    },
                },
            },
        }
    )


def test_opencode_event_stream_rejects_missing_or_fake_delegation() -> None:
    final = json.dumps(
        {
            "type": "text",
            "sessionID": "root-session",
            "part": {"type": "text", "text": "{}"},
        }
    )
    with pytest.raises(PolicyViolation, match="iki gercek delegated task"):
        parse_opencode_research_events(final)

    only_researcher = "\n".join([_task_event("zekam-researcher", "child-one", "call-1"), final])
    with pytest.raises(PolicyViolation, match="iki gercek delegated task"):
        parse_opencode_research_events(only_researcher)


def _valid_document() -> dict[str, object]:
    return {
        "schema": "zekam-opencode-research-result/v1",
        "question_digest": digest("question"),
        "researcher": {
            "agent_ref": "agent-a",
            "outcome": "success",
            "findings": [
                {
                    "finding_id": "finding-1",
                    "claim": "Kaynakta servis tanimlidir.",
                    "confidence": "high",
                    "citation_ids": ["chunk-1"],
                }
            ],
            "objections": [],
            "blocker": None,
        },
        "verification": {
            "verifier_ref": "agent-b",
            "researcher_payload_digest": digest(
                {
                    "agent_ref": "agent-a",
                    "outcome": "success",
                    "findings": [
                        {
                            "finding_id": "finding-1",
                            "claim": "Kaynakta servis tanimlidir.",
                            "confidence": "high",
                            "citation_ids": ["chunk-1"],
                        }
                    ],
                    "objections": [],
                    "blocker": None,
                }
            ),
            "evidence_manifest_digest": digest([]),
            "verified_finding_ids": ["finding-1"],
            "rejected_finding_ids": [],
            "rejection_reasons": [],
        },
        "grants_authority": False,
    }


def _validate(document: dict[str, object], evidence_manifest: object = []) -> None:
    validate_opencode_research_result(
        document,
        question_digest=digest("question"),
        allowed_citation_ids=frozenset({"chunk-1"}),
        evidence_manifest_digest=digest(evidence_manifest),
    )


def test_opencode_result_rejects_tampered_researcher_payload() -> None:
    """A02: koordinator researcher icerigini degistirirse payload digest uyusmaz."""
    document = _valid_document()
    assert isinstance(document["researcher"], dict)
    document["researcher"]["findings"][0]["claim"] = "Uydurulmus iddia."
    with pytest.raises(ValidationFailed, match="payload digest uyusmuyor"):
        _validate(document)


def test_opencode_result_rejects_hidden_verdict() -> None:
    """A03: rejected bulgu gizlenemez; verifier her finding icin karar vermeli."""
    document = _valid_document()
    assert isinstance(document["verification"], dict)
    document["verification"]["verified_finding_ids"] = []
    with pytest.raises(ValidationFailed, match="terminal karar"):
        _validate(document)


def test_opencode_result_rejects_digest_mismatch_verifier_input() -> None:
    """A04: verifier girdisi researcher payload ve evidence manifest digest'e bagli."""
    document = _valid_document()
    assert isinstance(document["verification"], dict)
    document["verification"]["evidence_manifest_digest"] = digest("baska-evidence")
    with pytest.raises(ValidationFailed, match="evidence manifest digest uyusmuyor"):
        _validate(document)


def test_opencode_result_rejects_nan_and_infinity() -> None:
    """A06: NaN/Infinity iceren payload deterministik olarak reddedilir."""
    document = _valid_document()
    assert isinstance(document["researcher"], dict)
    document["researcher"]["objections"] = [float("nan")]
    with pytest.raises(ValidationFailed):
        _validate(document)
    document["researcher"]["objections"] = [float("inf")]
    with pytest.raises(ValidationFailed):
        _validate(document)


def test_opencode_event_stream_binds_two_independent_completed_sessions() -> None:
    final = json.dumps(
        {
            "type": "text",
            "sessionID": "root-session",
            "part": {"type": "text", "text": "{}"},
        }
    )
    stream = "\n".join(
        [
            _task_event("zekam-researcher", "child-one", "call-1"),
            _task_event("zekam-verifier", "child-two", "call-2"),
            final,
        ]
    )
    _texts, execution = parse_opencode_research_events(stream)

    assert execution.root_session_id == "root-session"
    assert [item.session_id for item in execution.calls] == ["child-one", "child-two"]
    assert execution.as_dict()["delegated_agent_calls"] == 2

    model_document = {
        "researcher": {"agent_ref": "zekam-researcher:invented"},
        "verification": {"verifier_ref": "zekam-verifier:invented"},
    }
    bound = bind_opencode_result_document(model_document, execution)
    assert bound["researcher"]["agent_ref"] == "zekam-researcher:child-one"
    assert bound["verification"]["verifier_ref"] == "zekam-verifier:child-two"


def test_citation_tracks_original_and_delivered_digest(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A08: kesilmis excerpt'te original ve delivered digest ayridir."""
    home, store, project = _runtime(tmp_path, monkeypatch)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    result = run_research(
        store,
        home,
        plan,
        expected_run_digest=plan.run_digest,
        authorize_remote_query=True,
        authorize_agent_run=True,
        adapter=FakeAdapter(),
    )
    citation = result["report"]["findings"][0]["citations"][0]
    assert citation["source_content_digest"] == digest("class Demo")
    assert citation["content_digest"] == digest("class DemoMusteriService {}")
    assert citation["slice_digest"] == citation["content_digest"]


def test_research_run_rejects_tampered_citation_slice_digest(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A07: runtime citation slice_digest dogrulamasi reddeder."""
    from zekam.application import research_runtime as runtime_subject

    home, store, project = _runtime(tmp_path, monkeypatch)

    original_role_result = runtime_subject._role_result

    def tampered_role_result(result, by_citation):
        role, verification = original_role_result(result, by_citation)
        tampered_citation = role.findings[0].citations[0]
        tampered_finding = Finding(
            finding_id=role.findings[0].finding_id,
            claim=role.findings[0].claim,
            citations=(
                Citation(
                    snapshot_id=tampered_citation.snapshot_id,
                    locator_detail=tampered_citation.locator_detail,
                    content_digest=tampered_citation.content_digest,
                    source_content_digest=tampered_citation.source_content_digest,
                    slice_digest=digest("tampered-slice"),
                ),
            ),
            confidence=role.findings[0].confidence,
        )
        from zekam.domain.research import RoleResult

        return (
            RoleResult(
                role=role.role,
                agent_ref=role.agent_ref,
                outcome=role.outcome,
                payload_digest=role.payload_digest,
                findings=(tampered_finding,),
                objections=role.objections,
                blocker=role.blocker,
            ),
            verification,
        )

    monkeypatch.setattr(runtime_subject, "_role_result", tampered_role_result)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    with pytest.raises(ValidationFailed, match="slice_digest"):
        run_research(
            store,
            home,
            plan,
            expected_run_digest=plan.run_digest,
            authorize_remote_query=True,
            authorize_agent_run=True,
            adapter=FakeAdapter(),
        )


def test_legacy_report_replay_does_not_gain_new_verifier(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A45: old report replay keeps historical data and stays legacy-unverified."""
    from zekam.application.knowledge_file_plane import (
        KnowledgeClassification,
        generated_note_bytes,
        note_content_digest,
    )

    home, store, project = _runtime(tmp_path, monkeypatch)
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )

    # Create a legacy report without agent_execution evidence.
    legacy_report = {
        "schema": "zekam-research-report/v1",
        "report_id": "legacy-report-1",
        "question_id": plan.body["question"]["question_id"],
        "question_digest": plan.body["question_digest"],
        "status": "answered",
        "findings": [
            {
                "finding_id": "legacy-finding-1",
                "claim": "Tarihsel bulgu.",
                "citations": [
                    {
                        "snapshot_id": "legacy-snap-1",
                        "locator_detail": "line 1-5",
                        "content_digest": digest("legacy-body"),
                    }
                ],
                "confidence": "medium",
            }
        ],
        "unresolved_conflicts": [],
        "non_success_results": [],
        "verification": {
            "verifier_ref": "legacy-verifier",
            "researcher_payload_digest": digest("legacy-researcher"),
            "evidence_manifest_digest": digest([]),
            "verified_finding_ids": ["legacy-finding-1"],
            "rejected_finding_ids": [],
            "rejection_reasons": [],
        },
        "snapshots": [
            {
                "snapshot_id": "legacy-snap-1",
                "kind": "repository",
                "locator": "docs/Legacy.md",
                "content_digest": digest("legacy-body"),
                "revision": "abc123",
                "host": None,
            }
        ],
        "grants_authority": False,
    }
    report_without_digest = dict(legacy_report)
    report_digest = digest(report_without_digest)
    legacy_report["report_digest"] = report_digest

    # Materialize legacy projection.
    from zekam.application.knowledge_file_plane import KnowledgeNoteManifest
    from zekam.application.research_runtime import _LOCAL_REALM_ID

    markdown = subject._report_markdown(plan.body["question"]["question"], legacy_report)
    payload = generated_note_bytes(
        owner_scope=f"project:{plan.project_id}",
        project_slug=plan.project_slug,
        note_kind="research",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=("research-runs/legacy-job-1",),
        source_digests=(report_digest,),
        generated_at=subject._timestamp(),
        generator_version="zekam-research-runtime/v1",
        body=markdown,
    )
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
    files = KnowledgeFileStore(home)
    evidence_digest = digest(
        {
            "portable_ref": manifest.portable_ref,
            "content_digest": content_digest,
            "report_digest": report_digest,
        }
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
        uow.confirm_knowledge_note(
            note_id=note.id,
            expected_content_digest=content_digest,
            evidence_digest=evidence_digest,
        )
        uow.commit()

    # Enqueue a job and record a receipt tied to the legacy report digest.
    job, _created = runtime.enqueue(
        idempotency_key=str(plan.body["idempotency_key"]),
        payload=dict(plan.body) | {"dry_run": False},
        max_attempts=1,
    )
    work = runtime.claim_next(
        owner_id=f"research-{os.getpid()}",
        owner_pid=os.getpid(),
        owner_token=os.urandom(32).hex(),
        lease_seconds=600,
        resources=(f"research:{plan.run_digest}",),
        supported_operations=("research.run",),
        job_id=job.id,
    )
    claim, _created = runtime.claim_effect(
        work,
        operation="opencode.research",
        effect_digest=digest({"run_digest": plan.run_digest}),
        idempotency_key=f"research:{plan.run_digest}:effect",
    )
    runtime.record_receipt(claim, status="completed", evidence_digest=report_digest)
    runtime.finish(work, state="completed", evidence_digest=report_digest)

    surface = research_report(runtime, store, files, job.id)

    assert surface["verified"] is False
    assert surface["provenance"] == "legacy-unverified"
    assert surface["report"]["findings"][0]["finding_id"] == "legacy-finding-1"
    assert surface["report"]["report_digest"] == report_digest
    assert surface["grants_authority"] is False
