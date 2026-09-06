"""Process-isolated OpenCode adapter for bounded project research."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

RESULT_SCHEMA = "zekam-opencode-research-result/v1"
MAX_PROMPT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 20


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationFailed("OpenCode research JSON duplicate key tasiyor")
        result[key] = value
    return result


def _strict_json(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationFailed(f"{label} strict JSON olmali") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailed(f"{label} JSON object olmali")
    return parsed


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValidationFailed(f"{label} exact alan sozlesmesine uymuyor")


def _text(value: Any, label: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValidationFailed(f"{label} bounded non-empty text olmali")
    if len(value.encode("utf-8")) > maximum:
        raise ValidationFailed(f"{label} bounded siniri asiyor")
    if scan_text(value, relative_path="opencode-research-result.json"):
        raise PolicyViolation(f"{label} secret taramasini gecemedi")
    return value


def _string_list(value: Any, label: str, *, maximum: int = 50) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationFailed(f"{label} bounded string listesi olmali")
    result = tuple(_text(item, label, maximum=1000) for item in value)
    if len(set(result)) != len(result):
        raise ValidationFailed(f"{label} duplicate tasiyamaz")
    return result


@dataclass(frozen=True, slots=True)
class OpenCodeAgentCall:
    call_id: str
    agent_type: str
    parent_session_id: str
    session_id: str
    provider_id: str
    model_id: str
    input_digest: str
    output_digest: str

    def __post_init__(self) -> None:
        for label, value in (
            ("call id", self.call_id),
            ("agent type", self.agent_type),
            ("parent session", self.parent_session_id),
            ("session", self.session_id),
            ("provider", self.provider_id),
            ("model", self.model_id),
        ):
            _text(value, f"OpenCode {label}", maximum=200)
        parse_digest(self.input_digest)
        parse_digest(self.output_digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "call_id": self.call_id,
            "agent_type": self.agent_type,
            "parent_session_id": self.parent_session_id,
            "session_id": self.session_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
        }


@dataclass(frozen=True, slots=True)
class OpenCodeExecutionEvidence:
    root_session_id: str
    calls: tuple[OpenCodeAgentCall, ...]

    def __post_init__(self) -> None:
        _text(self.root_session_id, "OpenCode root session", maximum=200)
        if tuple(item.agent_type for item in self.calls) != (
            "zekam-researcher",
            "zekam-verifier",
        ):
            raise PolicyViolation("OpenCode iki gercek delegated task kaniti ister")
        if any(item.parent_session_id != self.root_session_id for item in self.calls):
            raise PolicyViolation("OpenCode task parent session baglantisi gecersiz")
        if any(item.session_id == self.root_session_id for item in self.calls):
            raise PolicyViolation("OpenCode task child session baglantisi gecersiz")
        if len({item.session_id for item in self.calls}) != len(self.calls):
            raise PolicyViolation("OpenCode subagent session kimlikleri bagimsiz olmali")
        if len({item.call_id for item in self.calls}) != len(self.calls):
            raise PolicyViolation("OpenCode task call kimlikleri tekil olmali")

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": "zekam-opencode-agent-execution/v1",
            "root_session_id": self.root_session_id,
            "calls": [item.as_dict() for item in self.calls],
            "root_agent_calls": 1,
            "delegated_agent_calls": len(self.calls),
        }
        return body | {"evidence_digest": digest(body)}


@dataclass(frozen=True, slots=True)
class OpenCodeResearchResult:
    document: dict[str, Any]
    researcher_ref: str
    verifier_ref: str
    outcome: str
    findings: tuple[dict[str, Any], ...]
    objections: tuple[str, ...]
    blocker: str | None
    verified_finding_ids: tuple[str, ...]
    rejected_finding_ids: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    execution: OpenCodeExecutionEvidence | None = None

    @property
    def root_agent_calls(self) -> int:
        return 1 if self.execution is not None else 0

    @property
    def delegated_agent_calls(self) -> int:
        return len(self.execution.calls) if self.execution is not None else 0


def require_opencode_execution(
    result: OpenCodeResearchResult,
) -> OpenCodeExecutionEvidence:
    execution = result.execution
    if execution is None:
        raise PolicyViolation("Research run OpenCode delegated task event kaniti ister")
    if tuple(item.agent_type for item in execution.calls) != (
        "zekam-researcher",
        "zekam-verifier",
    ):
        raise PolicyViolation("OpenCode iki gercek delegated task kaniti ister")
    expected_refs = {
        "zekam-researcher": result.researcher_ref,
        "zekam-verifier": result.verifier_ref,
    }
    for call in execution.calls:
        if expected_refs[call.agent_type] != f"{call.agent_type}:{call.session_id}":
            raise PolicyViolation("OpenCode result agent ref event session ile bagli degil")
    return execution


def parse_opencode_research_events(
    stream: str,
) -> tuple[tuple[str, ...], OpenCodeExecutionEvidence]:
    """Require an exact, completed researcher -> verifier task event chain."""

    text_events: list[str] = []
    root_session_id: str | None = None
    calls: list[OpenCodeAgentCall] = []
    for line in stream.splitlines():
        if not line.strip():
            continue
        event = _strict_json(line, "OpenCode research event")
        session_id = _text(event.get("sessionID"), "OpenCode root session", maximum=200)
        if root_session_id is None:
            root_session_id = session_id
        elif session_id != root_session_id:
            raise PolicyViolation("OpenCode event stream root session drift")
        event_type = event.get("type")
        part = event.get("part")
        if event_type == "text":
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    text_events.append(value)
            continue
        if event_type != "tool_use":
            continue
        if not isinstance(part, dict) or part.get("type") != "tool":
            raise ValidationFailed("OpenCode tool event sekli gecersiz")
        if part.get("tool") != "task":
            raise PolicyViolation("OpenCode research runner yalniz task araci kullanabilir")
        state = part.get("state")
        if not isinstance(state, dict) or state.get("status") != "completed":
            raise PolicyViolation("OpenCode delegated task terminal completed olmali")
        task_input = state.get("input")
        metadata = state.get("metadata")
        if not isinstance(task_input, dict) or not isinstance(metadata, dict):
            raise ValidationFailed("OpenCode task input/metadata eksik")
        agent_type = _text(task_input.get("subagent_type"), "OpenCode subagent type", maximum=100)
        expected = ("zekam-researcher", "zekam-verifier")
        if len(calls) >= len(expected) or agent_type != expected[len(calls)]:
            raise PolicyViolation("OpenCode task zinciri researcher -> verifier olmali")
        parent_session_id = _text(
            metadata.get("parentSessionId"), "OpenCode parent session", maximum=200
        )
        child_session_id = _text(metadata.get("sessionId"), "OpenCode child session", maximum=200)
        if parent_session_id != root_session_id or child_session_id == root_session_id:
            raise PolicyViolation("OpenCode task parent/child session baglantisi gecersiz")
        if any(item.session_id == child_session_id for item in calls):
            raise PolicyViolation("OpenCode subagent session kimlikleri bagimsiz olmali")
        if metadata.get("truncated") is not False:
            raise PolicyViolation("OpenCode task output truncated olamaz")
        model = metadata.get("model")
        if not isinstance(model, dict):
            raise ValidationFailed("OpenCode task model metadata eksik")
        output = _text(state.get("output"), "OpenCode task output", maximum=64 * 1024)
        call_id = _text(part.get("callID"), "OpenCode task call", maximum=200)
        calls.append(
            OpenCodeAgentCall(
                call_id=call_id,
                agent_type=agent_type,
                parent_session_id=parent_session_id,
                session_id=child_session_id,
                provider_id=_text(model.get("providerID"), "OpenCode provider", maximum=200),
                model_id=_text(model.get("modelID"), "OpenCode model", maximum=200),
                input_digest=digest(task_input),
                output_digest=digest(output),
            )
        )
    if root_session_id is None:
        raise ValidationFailed("OpenCode research event stream bos")
    if tuple(item.agent_type for item in calls) != ("zekam-researcher", "zekam-verifier"):
        raise PolicyViolation("OpenCode iki gercek delegated task kaniti ister")
    if not text_events:
        raise ValidationFailed("OpenCode research terminal text eventi uretmedi")
    return tuple(text_events), OpenCodeExecutionEvidence(root_session_id, tuple(calls))


def bind_opencode_result_document(
    document: dict[str, Any], execution: OpenCodeExecutionEvidence
) -> dict[str, Any]:
    """Replace model-authored identity labels with authoritative task event sessions."""

    bound = dict(document)
    researcher = bound.get("researcher")
    verification = bound.get("verification")
    if isinstance(researcher, dict):
        researcher = dict(researcher)
        researcher["agent_ref"] = f"zekam-researcher:{execution.calls[0].session_id}"
        bound["researcher"] = researcher
    if isinstance(verification, dict):
        verification = dict(verification)
        verification["verifier_ref"] = f"zekam-verifier:{execution.calls[1].session_id}"
        bound["verification"] = verification
    return bound


class OpenCodeResearchAdapter:
    """Invoke the managed primary runner and validate its only accepted output."""

    def __init__(
        self,
        *,
        executable: str = "opencode",
        cwd: Path,
        timeout_seconds: int = 600,
    ) -> None:
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValidationFailed("OpenCode research cwd absolute directory olmali")
        if not 1 <= timeout_seconds <= 600:
            raise ValidationFailed("OpenCode research timeout 1..600 olmali")
        self.executable = _text(executable, "OpenCode executable", maximum=1024)
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def execute(self, package: dict[str, Any]) -> OpenCodeResearchResult:
        prompt = "ZEKAM_RESEARCH_EXECUTION_V1\n" + canonical_json(package)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValidationFailed("OpenCode research prompt bounded siniri asiyor")
        if scan_text(prompt, relative_path="opencode-research-prompt.json"):
            raise PolicyViolation("OpenCode research prompt secret taramasini gecemedi")
        try:
            executable = _resolve_executable(self.executable)
            completed = subprocess.run(
                [
                    executable,
                    "run",
                    "--agent",
                    "zekam-research-runner",
                    "--format",
                    "json",
                    "--auto",
                    "--title",
                    "Zekam bounded research",
                    prompt,
                ],
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationFailed("OpenCode research bounded timeout") from exc
        except OSError as exc:
            raise ValidationFailed("OpenCode research process baslatilamadi") from exc
        if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
            raise ValidationFailed("OpenCode research output bounded siniri asiyor")
        if completed.returncode != 0:
            raise ValidationFailed(
                f"OpenCode research terminal hata verdi (exit={completed.returncode})"
            )
        try:
            stream = completed.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValidationFailed("OpenCode research event stream strict UTF-8 olmali") from exc
        text_events, execution = parse_opencode_research_events(stream)
        final_text = text_events[-1].strip()
        if scan_text(final_text, relative_path="opencode-research-result.json"):
            raise PolicyViolation("OpenCode research result secret taramasini gecemedi")
        document = bind_opencode_result_document(
            _strict_json(final_text, "OpenCode research result"), execution
        )
        result = validate_opencode_research_result(
            document,
            question_digest=str(package.get("question_digest", "")),
            allowed_citation_ids=frozenset(
                str(item.get("citation_id", ""))
                for item in package.get("evidence", [])
                if isinstance(item, dict)
            ),
        )
        result = replace(result, execution=execution)
        require_opencode_execution(result)
        return result


def _resolve_executable(value: str) -> str:
    """Resolve npm's Windows shim to its real binary without invoking a shell."""

    resolved = shutil.which(value)
    if resolved is None:
        candidate = Path(value)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise ValidationFailed("OpenCode executable bulunamadi")
    path = Path(resolved).resolve(strict=True)
    if path.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        target = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if path.stem.casefold() != "opencode" or not target.is_file():
            raise PolicyViolation("OpenCode shell shim guvenli binary'ye cozumlenemedi")
        path = target.resolve(strict=True)
    if path.suffix.casefold() != ".exe" and os.name == "nt":
        raise PolicyViolation("Windows OpenCode adapter direct executable ister")
    return str(path)


def validate_opencode_research_result(
    document: dict[str, Any],
    *,
    question_digest: str,
    allowed_citation_ids: frozenset[str],
) -> OpenCodeResearchResult:
    _exact_keys(
        document,
        {"schema", "question_digest", "researcher", "verification", "grants_authority"},
        "OpenCode research result",
    )
    if document["schema"] != RESULT_SCHEMA or document["question_digest"] != question_digest:
        raise ValidationFailed("OpenCode research result identity drift")
    if document["grants_authority"] is not False:
        raise PolicyViolation("OpenCode research result authority veremez")
    researcher = document["researcher"]
    verification = document["verification"]
    if not isinstance(researcher, dict) or not isinstance(verification, dict):
        raise ValidationFailed("OpenCode research nested object ister")
    _exact_keys(
        researcher,
        {"agent_ref", "outcome", "findings", "objections", "blocker"},
        "OpenCode researcher",
    )
    _exact_keys(
        verification,
        {
            "verifier_ref",
            "verified_finding_ids",
            "rejected_finding_ids",
            "rejection_reasons",
        },
        "OpenCode verifier",
    )
    researcher_ref = _text(researcher["agent_ref"], "Researcher ref", maximum=200)
    verifier_ref = _text(verification["verifier_ref"], "Verifier ref", maximum=200)
    if researcher_ref == verifier_ref:
        raise PolicyViolation("OpenCode verifier researcher'dan bagimsiz olmali")
    outcome = researcher["outcome"]
    allowed_outcomes = {
        "success",
        "partial",
        "failed",
        "blocked",
        "abstained",
        "recovery-required",
    }
    if outcome not in allowed_outcomes:
        raise ValidationFailed("OpenCode researcher outcome gecersiz")
    raw_findings = researcher["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
        raise ValidationFailed("OpenCode findings bounded liste olmali")
    findings: list[dict[str, Any]] = []
    finding_ids: set[str] = set()
    for raw in raw_findings:
        if not isinstance(raw, dict):
            raise ValidationFailed("OpenCode finding object olmali")
        _exact_keys(raw, {"finding_id", "claim", "confidence", "citation_ids"}, "Finding")
        finding_id = _text(raw["finding_id"], "Finding id", maximum=200)
        if finding_id in finding_ids:
            raise ValidationFailed("OpenCode finding id duplicate olamaz")
        finding_ids.add(finding_id)
        confidence = raw["confidence"]
        if confidence not in {"low", "medium", "high"}:
            raise ValidationFailed("OpenCode finding confidence gecersiz")
        citation_ids = _string_list(raw["citation_ids"], "Citation id", maximum=20)
        if not citation_ids or not set(citation_ids) <= allowed_citation_ids:
            raise PolicyViolation("OpenCode finding yalniz known citation kullanabilir")
        findings.append(
            {
                "finding_id": finding_id,
                "claim": _text(raw["claim"], "Finding claim", maximum=4000),
                "confidence": confidence,
                "citation_ids": citation_ids,
            }
        )
    if outcome == "success" and not findings:
        raise ValidationFailed("OpenCode success en az bir finding ister")
    blocker_value = researcher["blocker"]
    blocker = None if blocker_value is None else _text(blocker_value, "Research blocker")
    if outcome in {"blocked", "recovery-required"} and blocker is None:
        raise ValidationFailed("OpenCode blocked outcome gerekce ister")
    objections = _string_list(researcher["objections"], "Research objection")
    verified = _string_list(verification["verified_finding_ids"], "Verified finding id")
    rejected = _string_list(verification["rejected_finding_ids"], "Rejected finding id")
    reasons = _string_list(verification["rejection_reasons"], "Rejection reason")
    if len(rejected) != len(reasons) or set(verified) & set(rejected):
        raise ValidationFailed("OpenCode verification cardinality/overlap drift")
    if not set(verified + rejected) <= finding_ids:
        raise ValidationFailed("OpenCode verification unknown finding tasiyor")
    if set(verified + rejected) != finding_ids:
        raise ValidationFailed("OpenCode verifier her finding icin terminal karar vermeli")
    return OpenCodeResearchResult(
        document=document,
        researcher_ref=researcher_ref,
        verifier_ref=verifier_ref,
        outcome=outcome,
        findings=tuple(findings),
        objections=objections,
        blocker=blocker,
        verified_finding_ids=verified,
        rejected_finding_ids=rejected,
        rejection_reasons=reasons,
    )
