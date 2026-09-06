from __future__ import annotations

import json
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
        return OpenCodeResearchResult(
            document={},
            researcher_ref="zekam-researcher:run-1",
            verifier_ref="zekam-verifier:run-2",
            outcome="success",
            findings=(
                {
                    "finding_id": "finding-1",
                    "claim": "Musteri servisi kaynakta tanimlidir.",
                    "confidence": "high",
                    "citation_ids": (citation_id,),
                },
            ),
            objections=(),
            blocker=None,
            verified_finding_ids=("finding-1",),
            rejected_finding_ids=(),
            rejection_reasons=(),
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
            "verified_finding_ids": ["finding-1"],
            "rejected_finding_ids": [],
            "rejection_reasons": [],
        },
        "grants_authority": False,
    }
    with pytest.raises(PolicyViolation, match="known citation"):
        validate_opencode_research_result(
            document,
            question_digest=digest("question"),
            allowed_citation_ids=frozenset({"chunk-1"}),
        )

    document["researcher"]["findings"][0]["citation_ids"] = ["chunk-1"]
    document["verification"]["verifier_ref"] = "agent-a"
    with pytest.raises(PolicyViolation, match="bagimsiz"):
        validate_opencode_research_result(
            document,
            question_digest=digest("question"),
            allowed_citation_ids=frozenset({"chunk-1"}),
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
