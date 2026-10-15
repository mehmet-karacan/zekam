from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application import research_runtime as subject
from zekam.application.home import HomeLayout
from zekam.application.research_runtime import (
    ResearchRunPlan,
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
        "project_rag_source_freshness",
        lambda *_: {
            "state": "current",
            "index_freshness": "current",
            "stale_reasons": [],
            "source_revision": revision,
            "generation_digest": generation,
            "read_only": True,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        },
    )
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": generation,
            "citations": [{"chunk_id": "chunk-1"}],
            "index_freshness": "current",
            "stale_reasons": [],
            "snapshot_only": False,
            "reindex_recommended": False,
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
    assert report["report"]["agent_execution"]["usage_by_model"] == [
        {
            "model_ref": "test/test-model",
            "provider_calls": 2,
            "tokens": None,
            "duration_seconds": None,
            "cost_micros": None,
            "disk_bytes": None,
            "metrics_state": "unknown",
            "measurement_reason": "OpenCode execution event usage alani tasimiyor",
        },
    ]
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
            "index_freshness": "current",
            "stale_reasons": [],
            "snapshot_only": False,
            "reindex_recommended": False,
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


def _plan_with_evidence_budget(
    plan: ResearchRunPlan, *, maximum_items: int, maximum_chars: int
) -> ResearchRunPlan:
    body = json.loads(json.dumps(plan.body))
    body["retrieval"]["evidence_budget"] = {
        "maximum_items": maximum_items,
        "maximum_chars": maximum_chars,
    }
    stable = {
        key: value
        for key, value in body.items()
        if key not in {"run_digest", "idempotency_key", "projection_ref", "dry_run"}
    }
    run_digest = digest(stable)
    body.update(
        {
            "run_digest": run_digest,
            "idempotency_key": f"research:{run_digest}",
            "projection_ref": (
                f"projeler/{plan.project_slug}/arastirmalar/generated/{run_digest[7:]}.md"
            ),
            "dry_run": True,
        }
    )
    return ResearchRunPlan(body)


def _legacy_plan_without_evidence_budget(plan: ResearchRunPlan) -> ResearchRunPlan:
    body = json.loads(json.dumps(plan.body))
    body["retrieval"].pop("evidence_budget", None)
    stable = {
        key: value
        for key, value in body.items()
        if key not in {"run_digest", "idempotency_key", "projection_ref", "dry_run"}
    }
    run_digest = digest(stable)
    body.update(
        {
            "run_digest": run_digest,
            "idempotency_key": f"research:{run_digest}",
            "projection_ref": (
                f"projeler/{plan.project_slug}/arastirmalar/generated/{run_digest[7:]}.md"
            ),
            "dry_run": True,
        }
    )
    return ResearchRunPlan(body)


def test_research_evidence_budget_reads_more_than_legacy_five(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    citations = [{"chunk_id": f"chunk-{index}"} for index in range(1, 7)]
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": citations,
            "index_freshness": "current",
            "stale_reasons": [],
            "snapshot_only": False,
            "reindex_recommended": False,
        },
    )

    def read_citation(_home, _slug, citation_id, **_kwargs):  # type: ignore[no-untyped-def]
        body = f"class Demo{citation_id.removeprefix('chunk-')} {{}}"
        return {
            "source_ref": f"src/main/Demo{citation_id}.java",
            "source_revision": digest("revision"),
            "content_digest": digest(body),
            "locator": {"relative_path": f"src/main/Demo{citation_id}.java", "start_line": 1},
            "body": body,
        }

    monkeypatch.setattr(subject, "read_project_citation", read_citation)

    class ObservingAdapter(FakeAdapter):
        package: dict[str, object] | None = None

        def execute(self, package):  # type: ignore[no-untyped-def]
            self.package = package
            return super().execute(package)

    adapter = ObservingAdapter()
    plan = _legacy_plan_without_evidence_budget(
        build_research_run_plan(
            store, home, project_ref=project.slug, question="Musteri servisi nerede?"
        )
    )
    result = run_research(
        store,
        home,
        plan,
        expected_run_digest=plan.run_digest,
        authorize_remote_query=True,
        authorize_agent_run=True,
        adapter=adapter,
    )

    assert adapter.package is not None
    assert len(adapter.package["evidence"]) == 6  # type: ignore[index]
    coverage = result["report"]["evidence_coverage"]
    assert coverage["state"] == "complete"
    assert coverage["requested_items"] == 6
    assert coverage["selected_items"] == 6
    assert result["report"]["status"] == "answered"


def test_research_evidence_budget_gap_is_explicitly_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    citations = [{"chunk_id": f"chunk-{index}"} for index in range(1, 5)]
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": citations,
            "index_freshness": "current",
            "stale_reasons": [],
            "snapshot_only": False,
            "reindex_recommended": False,
            "evidence_sufficient": False,
            "uncovered_identifiers": ["MusteriService"],
        },
    )

    def read_citation(_home, _slug, citation_id, **_kwargs):  # type: ignore[no-untyped-def]
        body = f"evidence-{citation_id}-" + ("x" * 40)
        return {
            "source_ref": f"src/main/{citation_id}.java",
            "source_revision": digest("revision"),
            "content_digest": digest(body),
            "locator": {"relative_path": f"src/main/{citation_id}.java", "start_line": 1},
            "body": body,
        }

    monkeypatch.setattr(subject, "read_project_citation", read_citation)
    plan = _plan_with_evidence_budget(
        build_research_run_plan(
            store, home, project_ref=project.slug, question="Musteri servisi nerede?"
        ),
        maximum_items=2,
        maximum_chars=32,
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

    coverage = result["report"]["evidence_coverage"]
    assert coverage["state"] == "partial"
    assert coverage["selected_items"] == 1
    assert coverage["omitted_items"] == 3
    assert "evidence-budget-exhausted" in coverage["gaps"]
    assert "retrieval-evidence-gap" in coverage["gaps"]
    assert coverage["uncovered_identifiers"] == ["MusteriService"]
    assert result["report"]["status"] == "partial"


def test_research_query_staleness_is_carried_into_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": [{"chunk_id": "chunk-1"}],
            "index_freshness": "stale",
            "stale_reasons": ["project-source-revision-stale"],
            "snapshot_only": True,
            "reindex_recommended": True,
        },
    )
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

    coverage = result["report"]["evidence_coverage"]
    assert coverage["index_freshness"] == "stale"
    assert coverage["stale_reasons"] == ["project-source-revision-stale"]
    assert coverage["snapshot_only"] is True
    assert coverage["reindex_recommended"] is True
    assert "retrieval-index-stale" in coverage["gaps"]
    assert coverage["state"] == "partial"
    assert result["report"]["status"] == "partial"


def test_research_query_missing_freshness_is_explicit_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": [{"chunk_id": "chunk-1"}],
        },
    )
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
    coverage = result["report"]["evidence_coverage"]
    assert coverage["index_freshness"] == "unknown"
    assert "retrieval-freshness-unavailable" in coverage["gaps"]
    assert coverage["state"] == "partial"
    assert result["report"]["status"] == "partial"


def test_research_objections_become_unresolved_evidence_conflicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)

    class ObjectionAdapter(FakeAdapter):
        def execute(self, package):  # type: ignore[no-untyped-def]
            result = super().execute(package)
            return replace(result, objections=("Kayitlar arasinda kapsam boslugu var.",))

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
        adapter=ObjectionAdapter(),
    )

    conflicts = result["report"]["unresolved_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "evidence-gap"
    assert conflicts[0]["detail"] == "Kayitlar arasinda kapsam boslugu var."
    assert result["report"]["status"] == "partial"


def test_research_run_rejects_live_source_revision_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    monkeypatch.setattr(
        subject,
        "project_rag_status",
        lambda *_args, **_kwargs: {
            "state": "ready",
            "generation_digest": digest("generation"),
            "source_revision": digest("live-drift"),
        },
    )

    with pytest.raises(PolicyViolation, match="stale arastirma sorusu"):
        run_research(
            store,
            home,
            plan,
            expected_run_digest=plan.run_digest,
            authorize_remote_query=True,
            authorize_agent_run=True,
            adapter=FakeAdapter(),
        )


def test_research_run_marks_post_citation_source_drift_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    monkeypatch.setattr(
        subject,
        "project_rag_source_freshness",
        lambda *_args, **_kwargs: {
            "state": "stale",
            "index_freshness": "stale",
            "stale_reasons": ["project-source-content-stale"],
        },
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

    coverage = result["report"]["evidence_coverage"]
    assert coverage["state"] == "partial"
    assert "retrieval-source-drift-after-citation" in coverage["gaps"]
    assert coverage["post_citation_freshness"]["state"] == "stale"
    assert result["report"]["status"] == "partial"


def test_research_evidence_empty_result_is_explicit_evidence_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )

    evidence, snapshots, by_citation, coverage = subject._bounded_evidence(
        home,
        plan,
        {"citations": []},
    )

    assert evidence == ()
    assert snapshots == ()
    assert by_citation == {}
    assert coverage["state"] == "evidence-gap"
    assert coverage["gaps"] == ["no-citations"]


def test_research_evidence_budget_still_scans_secret_after_legacy_fifth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    citations = [{"chunk_id": f"chunk-{index}"} for index in range(1, 7)]
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": citations,
            "index_freshness": "current",
            "stale_reasons": [],
            "snapshot_only": False,
            "reindex_recommended": False,
        },
    )
    read_ids: list[str] = []

    def read_citation(_home, _slug, citation_id, **_kwargs):  # type: ignore[no-untyped-def]
        read_ids.append(citation_id)
        body = f"safe-{citation_id}"
        if citation_id == "chunk-6":
            body = "pass" + "word = 'regression-secret'"
        return {
            "source_ref": f"src/main/{citation_id}.java",
            "source_revision": digest("revision"),
            "content_digest": digest(body),
            "locator": {"relative_path": f"src/main/{citation_id}.java", "start_line": 1},
            "body": body,
        }

    monkeypatch.setattr(subject, "read_project_citation", read_citation)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )

    with pytest.raises(PolicyViolation, match="secret"):
        run_research(
            store,
            home,
            plan,
            expected_run_digest=plan.run_digest,
            authorize_remote_query=True,
            authorize_agent_run=True,
            adapter=FakeAdapter(),
        )
    assert read_ids == [f"chunk-{index}" for index in range(1, 7)]


def test_research_evidence_budget_preserves_freshness_gate_after_legacy_fifth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, store, project = _runtime(tmp_path, monkeypatch)
    citations = [{"chunk_id": f"chunk-{index}"} for index in range(1, 7)]
    monkeypatch.setattr(
        subject,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "answered",
            "generation_digest": digest("generation"),
            "citations": citations,
            "index_freshness": "current",
            "stale_reasons": [],
            "snapshot_only": False,
            "reindex_recommended": False,
        },
    )
    read_ids: list[str] = []

    def read_citation(_home, _slug, citation_id, **_kwargs):  # type: ignore[no-untyped-def]
        read_ids.append(citation_id)
        body = f"safe-{citation_id}"
        return {
            "source_ref": f"src/main/{citation_id}.java",
            "source_revision": (
                digest("stale-revision") if citation_id == "chunk-6" else digest("revision")
            ),
            "content_digest": digest(body),
            "locator": {"relative_path": f"src/main/{citation_id}.java", "start_line": 1},
            "body": body,
        }

    monkeypatch.setattr(subject, "read_project_citation", read_citation)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )

    with pytest.raises(PolicyViolation, match="source revision"):
        run_research(
            store,
            home,
            plan,
            expected_run_digest=plan.run_digest,
            authorize_remote_query=True,
            authorize_agent_run=True,
            adapter=FakeAdapter(),
        )
    assert read_ids == [f"chunk-{index}" for index in range(1, 7)]


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


def test_research_resumes_durable_job_interrupted_before_first_lease(monkeypatch, tmp_path: Path):
    home, store, project = _runtime(tmp_path, monkeypatch)
    plan = build_research_run_plan(
        store, home, project_ref=project.slug, question="Musteri servisi nerede?"
    )
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    queued, created = runtime.enqueue(
        idempotency_key=plan.body["idempotency_key"],
        payload=plan.body | {"dry_run": False},
        max_attempts=1,
    )
    assert created and queued.state == "ready" and queued.attempt_count == 0

    class CountingAdapter(FakeAdapter):
        calls = 0

        def execute(self, package):
            self.calls += 1
            return super().execute(package)

    adapter = CountingAdapter()
    for _ in range(2):
        result = run_research(
            store,
            home,
            plan,
            expected_run_digest=plan.run_digest,
            authorize_remote_query=True,
            authorize_agent_run=True,
            adapter=adapter,
        )
        assert result["job_id"] == queued.id and result["state"] == "completed"
    snapshot = runtime.job_snapshot(queued.id)
    assert adapter.calls == 1 and snapshot["attempt_count"] == 1
    assert len(snapshot["effects"]) == 1
    assert snapshot["effects"][0]["receipt_status"] == "completed"
