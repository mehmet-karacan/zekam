"""Offline host bridge fixtures do not qualify real native hosts."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from zekam.domain.canonical import digest
from zekam.domain.clients import ClientCapabilityManifest, ClientKind
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.native_research import (
    NativeResearchAdapter,
    NativeResearchCall,
    require_research_execution,
)


class HostFixture:
    root_session_id = "fixture-root"

    def __init__(self, kind=ClientKind.CODEX, fault=None):
        self.capability = ClientCapabilityManifest(
            "fixture-native",
            kind,
            "fixture-v1",
            ("cancellation", "child-dispatch", "structured-result", "tool-use"),
        )
        self.tasks = []
        self.fault = fault

    def execute_child(self, task, *, timeout_seconds, cancel):
        self.tasks.append(task)
        role = task["role"]
        if role == "researcher":
            payload = {
                "outcome": "success",
                "findings": [
                    {
                        "finding_id": "f1",
                        "claim": "Fixture claim.",
                        "confidence": "high",
                        "citation_ids": ["citation-1"],
                    }
                ],
                "objections": [],
                "blocker": None,
            }
        else:
            payload = {
                "researcher_payload_digest": task["researcher_payload_digest"],
                "evidence_manifest_digest": task["evidence_manifest_digest"],
                "verified_finding_ids": ["f1"],
                "rejected_finding_ids": [],
                "rejection_reasons": [],
            }
            if self.fault == "wrong-subject":
                payload["researcher_payload_digest"] = digest("other-subject")
        result = NativeResearchCall(
            "call-" + role,
            role,
            self.root_session_id,
            "child-shared" if self.fault == "same-session" else "child-" + role,
            "fixture-provider",
            "fixture-model",
            digest(task),
            digest(payload),
            digest(self.capability.as_dict()),
            digest("fixture-catalog"),
            payload,
        )
        if self.fault == "wrong-input":
            result = replace(result, input_digest=digest("another-task"))
        if self.fault == "mutated-task":
            task["question"] = "changed question"
        if self.fault == "late-cancel":
            cancel.set()
        return result


def _package():
    return {
        "question": "Fixture question?",
        "question_digest": digest("fixture-question"),
        "project_slug": "fixture",
        "source_revision": "fixture-revision",
        "evidence": [{"citation_id": "citation-1", "excerpt": "Fixture evidence."}],
        "scope": {
            "project_id": "00000000-0000-0000-0000-000000000001",
            "run_digest": digest("fixture-run"),
        },
        "budget": {"max_tokens": 12000, "max_cost_units": 3, "max_seconds": 600, "max_rounds": 1},
    }


@pytest.mark.parametrize("kind", [ClientKind.CODEX, ClientKind.CLAUDE_CODE])
def test_native_host_roles_produce_bounded_evidence_and_shared_report_contract(kind) -> None:
    host = HostFixture(kind)
    result = NativeResearchAdapter(host).execute(_package())
    execution = require_research_execution(result)
    assert result.root_agent_calls == 0
    assert result.delegated_agent_calls == 2
    assert result.document["schema"] == "zekam-research-result/v1"
    assert result.verified_finding_ids == ("f1",)
    assert [task["role"] for task in host.tasks] == ["researcher", "verifier"]
    assert execution.as_dict()["client"]["kind"] == kind.value
    assert "payload" not in execution.as_dict()["calls"][0]
    assert execution.as_dict()["grants_authority"] is False
    assert host.tasks[1]["researcher_payload_digest"] == result.researcher_payload_digest
    assert sum(task["budget"]["max_tokens"] for task in host.tasks) <= 12000
    assert all(task["execution_policy"]["write_resources"] == [] for task in host.tasks)


def test_native_host_missing_child_capability_does_not_dispatch() -> None:
    host = HostFixture()
    host.capability = replace(host.capability, capabilities=("structured-result", "tool-use"))
    with pytest.raises(PolicyViolation, match="capability"):
        NativeResearchAdapter(host)
    assert host.tasks == []


@pytest.mark.parametrize(
    "fault", ["wrong-subject", "same-session", "wrong-input", "mutated-task", "late-cancel"]
)
def test_native_host_bad_subject_identity_or_cancel_is_not_accepted(fault) -> None:
    host = HostFixture(fault=fault)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        NativeResearchAdapter(host, cancel=threading.Event()).execute(_package())


def test_native_result_cannot_replace_observed_host_payload() -> None:
    result = NativeResearchAdapter(HostFixture()).execute(_package())
    with pytest.raises((PolicyViolation, ValidationFailed)):
        require_research_execution(replace(result, findings=()))


def test_native_oversized_evidence_rejected_before_child_call() -> None:
    host = HostFixture()
    package = _package()
    package["evidence"][0]["excerpt"] = "x" * 70000
    with pytest.raises(PolicyViolation, match="bounded"):
        NativeResearchAdapter(host).execute(package)
    assert host.tasks == []
