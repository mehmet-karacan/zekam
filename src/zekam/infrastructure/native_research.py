"""Research bridge for host-owned child tools; never synthesizes delegation."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID

from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.clients import ClientCapabilityManifest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.opencode_research import (
    MAX_PROMPT_BYTES,
    OpenCodeExecutionEvidence,
    OpenCodeResearchResult,
    require_opencode_execution,
    validate_opencode_research_result,
)


@dataclass(frozen=True, slots=True)
class NativeResearchCall:
    """Host observation from a trusted bridge, never parsed from model text."""

    call_id: str
    agent_type: str
    parent_session_id: str
    session_id: str
    provider_id: str
    model_id: str
    input_digest: str
    output_digest: str
    capability_digest: str
    model_catalog_digest: str
    payload: dict[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.agent_type, str) or self.agent_type not in {
            "researcher",
            "verifier",
        }:
            raise ValidationFailed("Native research child role gecersiz")
        for value in (
            self.call_id,
            self.parent_session_id,
            self.session_id,
            self.provider_id,
            self.model_id,
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or len(value.encode("utf-8")) > 200
                or any(ord(char) < 32 for char in value)
                or scan_text(value, relative_path="native-research-identity")
            ):
                raise ValidationFailed("Native research host identity gecersiz")
        for value in (
            self.input_digest,
            self.output_digest,
            self.capability_digest,
            self.model_catalog_digest,
        ):
            if not isinstance(value, str):
                raise ValidationFailed("Native research digest metin olmali")
            parse_digest(value)
        if (
            not isinstance(self.payload, dict)
            or len(canonical_json(self.payload).encode("utf-8")) > MAX_PROMPT_BYTES
            or digest(self.payload) != self.output_digest
            or scan_text(canonical_json(self.payload), relative_path="native-research-result")
        ):
            raise PolicyViolation("Native research child payload/digest gecersiz")

    def as_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in (
                "call_id",
                "agent_type",
                "parent_session_id",
                "session_id",
                "provider_id",
                "model_id",
                "input_digest",
                "output_digest",
                "capability_digest",
                "model_catalog_digest",
            )
        }


@dataclass(frozen=True, slots=True)
class NativeResearchExecution:
    capability: ClientCapabilityManifest
    root_session_id: str
    question_digest: str
    calls: tuple[NativeResearchCall, ...]
    root_agent_calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        required = {"child-dispatch", "structured-result", "tool-use", "cancellation"}
        if type(self.capability) is not ClientCapabilityManifest or not required <= set(
            self.capability.capabilities
        ):
            raise PolicyViolation("Native research gercek child-dispatch capability ister")
        parse_digest(self.question_digest)
        if (
            not isinstance(self.calls, tuple)
            or len(self.calls) != 2
            or any(type(call) is not NativeResearchCall for call in self.calls)
        ):
            raise PolicyViolation("Native research iki gercek child sonucu ister")
        if tuple(call.agent_type for call in self.calls) != ("researcher", "verifier"):
            raise PolicyViolation("Native research role sirasi gecersiz")
        for call in self.calls:
            call.__post_init__()
        if (
            any(
                call.parent_session_id != self.root_session_id
                or call.session_id == self.root_session_id
                for call in self.calls
            )
            or len({call.call_id for call in self.calls}) != 2
            or len({call.session_id for call in self.calls}) != 2
            or len({call.model_catalog_digest for call in self.calls}) != 1
            or any(
                call.capability_digest != digest(self.capability.as_dict()) for call in self.calls
            )
        ):
            raise PolicyViolation("Native research child identity/capability drift")

    def as_dict(self) -> dict[str, Any]:
        body = {
            "schema": "zekam-native-research-execution/v1",
            "client": self.capability.as_dict(),
            "root_session_id": self.root_session_id,
            "question_digest": self.question_digest,
            "root_agent_calls": 0,
            "delegated_agent_calls": 2,
            "calls": [call.as_dict() for call in self.calls],
            "grants_authority": False,
        }
        return body | {"evidence_digest": digest(body)}


class NativeResearchHost(Protocol):
    """Trusted native bridge: observe actual calls and cancel its own children."""

    capability: ClientCapabilityManifest
    root_session_id: str

    def execute_child(
        self, task: dict[str, Any], *, timeout_seconds: int, cancel: threading.Event | None
    ) -> NativeResearchCall: ...


class NativeResearchAdapter:
    def __init__(
        self,
        host: NativeResearchHost,
        *,
        cancel: threading.Event | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            raise ValidationFailed("Native research timeout 1..600 olmali")
        if type(host.capability) is not ClientCapabilityManifest or not {
            "child-dispatch",
            "structured-result",
            "tool-use",
            "cancellation",
        } <= set(host.capability.capabilities):
            raise PolicyViolation("Native research host child capability eksik")
        self.host, self.cancel, self.timeout_seconds = host, cancel, timeout_seconds
        if (
            not isinstance(host.root_session_id, str)
            or not host.root_session_id.strip()
            or len(host.root_session_id.encode("utf-8")) > 200
        ):
            raise ValidationFailed("Native research root session gecersiz")

    def execute(self, package: dict[str, Any]) -> OpenCodeResearchResult:
        deadline = time.monotonic() + self.timeout_seconds
        capability_digest = digest(self.host.capability.as_dict())
        root_session_id = self.host.root_session_id
        question_digest = package.get("question_digest")
        if not isinstance(question_digest, str):
            raise ValidationFailed("Native research question digest gerekli")
        parse_digest(question_digest)
        evidence = package.get("evidence")
        if not isinstance(evidence, list):
            raise ValidationFailed("Native research bounded evidence listesi ister")
        if (
            not 1 <= len(evidence) <= 64
            or any(
                not isinstance(item, dict) or not isinstance(item.get("citation_id"), str)
                for item in evidence
            )
            or len({item["citation_id"] for item in evidence}) != len(evidence)
        ):
            raise ValidationFailed("Native research unique bounded citation evidence gerekli")
        scope = package.get("scope")
        if not isinstance(scope, dict) or set(scope) != {"project_id", "run_digest"}:
            raise ValidationFailed("Native research project/run scope gerekli")
        try:
            if str(UUID(scope["project_id"])) != scope["project_id"]:
                raise ValueError("canonical project")
            parse_digest(scope["run_digest"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationFailed("Native research project/run scope gecersiz") from exc
        evidence_digest = digest(evidence)
        budget = package.get("budget")
        if (
            not isinstance(budget, dict)
            or set(budget) != {"max_tokens", "max_cost_units", "max_seconds", "max_rounds"}
            or any(type(value) is not int or value < 1 for value in budget.values())
            or budget["max_tokens"] < 2
            or budget["max_tokens"] > 12000
            or budget["max_cost_units"] < 2
            or budget["max_cost_units"] > 3
            or budget["max_seconds"] > 600
            or budget["max_rounds"] != 1
        ):
            raise ValidationFailed("Native research bounded child budget gerekli")
        deadline = min(deadline, time.monotonic() + budget["max_seconds"])
        base = {
            "schema": "zekam-native-research-task/v1",
            "question": package.get("question"),
            "question_digest": question_digest,
            "project_slug": package.get("project_slug"),
            "source_revision": package.get("source_revision"),
            "evidence": evidence,
            "scope": package.get("scope"),
            "budget": {
                "max_tokens": budget["max_tokens"] // 2,
                "max_cost_units": budget["max_cost_units"] // 2,
                "maximum_child_calls": 1,
                "max_rounds": 1,
            },
            "execution_policy": {
                "source_access": "provided-evidence-only",
                "write_resources": [],
                "network_access": False,
            },
            "grants_authority": False,
        }

        def dispatch(task: dict[str, Any], role: str) -> NativeResearchCall:
            if self.cancel is not None and self.cancel.is_set():
                raise PolicyViolation("Native research iptal edildi")
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                raise PolicyViolation("Native research toplam sure butcesi bitti")
            serialized = canonical_json(task)
            if len(serialized.encode("utf-8")) > MAX_PROMPT_BYTES or scan_text(
                serialized, relative_path="native-research-task"
            ):
                raise PolicyViolation("Native research task bounded/secret kapisi")
            task_digest = digest(task)
            call = self.host.execute_child(task, timeout_seconds=remaining, cancel=self.cancel)
            if (
                type(call) is not NativeResearchCall
                or call.agent_type != role
                or call.input_digest != task_digest
                or digest(task) != task_digest
                or call.output_digest != digest(call.payload)
                or call.capability_digest != capability_digest
                or call.parent_session_id != root_session_id
                or self.host.root_session_id != root_session_id
                or digest(self.host.capability.as_dict()) != capability_digest
            ):
                raise PolicyViolation("Native research host call binding drift")
            if time.monotonic() >= deadline or (self.cancel is not None and self.cancel.is_set()):
                raise PolicyViolation("Native research gec child sonucu reddedildi")
            return call

        first = dispatch(
            base
            | {
                "role": "researcher",
                "result_fields": ["outcome", "findings", "objections", "blocker"],
            },
            "researcher",
        )
        if set(first.payload) != {"outcome", "findings", "objections", "blocker"}:
            raise ValidationFailed("Native researcher exact payload gerekli")
        researcher = first.payload | {"agent_ref": f"researcher:{first.session_id}"}
        second = dispatch(
            base
            | {
                "role": "verifier",
                "researcher": researcher,
                "researcher_payload_digest": digest(researcher),
                "evidence_manifest_digest": evidence_digest,
                "result_fields": [
                    "researcher_payload_digest",
                    "evidence_manifest_digest",
                    "verified_finding_ids",
                    "rejected_finding_ids",
                    "rejection_reasons",
                ],
            },
            "verifier",
        )
        execution = NativeResearchExecution(
            self.host.capability, root_session_id, question_digest, (first, second)
        )
        document = {
            "schema": "zekam-research-result/v1",
            "question_digest": question_digest,
            "researcher": researcher,
            "verification": second.payload | {"verifier_ref": f"verifier:{second.session_id}"},
            "grants_authority": False,
        }
        result = validate_opencode_research_result(
            document,
            question_digest=question_digest,
            allowed_citation_ids=frozenset(item["citation_id"] for item in evidence),
            evidence_manifest_digest=evidence_digest,
            result_schema="zekam-research-result/v1",
        )
        return replace(result, execution=execution)


def require_research_execution(
    result: OpenCodeResearchResult,
) -> OpenCodeExecutionEvidence | NativeResearchExecution:
    if type(result.execution) is NativeResearchExecution:
        execution = result.execution
        execution.__post_init__()
        if (
            execution.question_digest != result.document.get("question_digest")
            or result.researcher_ref != f"researcher:{execution.calls[0].session_id}"
            or result.verifier_ref != f"verifier:{execution.calls[1].session_id}"
        ):
            raise PolicyViolation("Native research result/execution scope drift")
        expected_document = {
            "schema": "zekam-research-result/v1",
            "question_digest": execution.question_digest,
            "researcher": execution.calls[0].payload | {"agent_ref": result.researcher_ref},
            "verification": execution.calls[1].payload | {"verifier_ref": result.verifier_ref},
            "grants_authority": False,
        }
        if result.document != expected_document:
            raise PolicyViolation("Native research document observed child payload ile eslesmiyor")
        checked = validate_opencode_research_result(
            expected_document,
            question_digest=execution.question_digest,
            allowed_citation_ids=frozenset(
                citation for finding in result.findings for citation in finding["citation_ids"]
            ),
            evidence_manifest_digest=result.evidence_manifest_digest,
            result_schema="zekam-research-result/v1",
        )
        if replace(checked, execution=execution) != result:
            raise PolicyViolation("Native research typed result/document drift")
        return execution
    if type(result.execution) is not OpenCodeExecutionEvidence:
        raise PolicyViolation("Research gercek host execution kaniti ister")
    return require_opencode_execution(result)
