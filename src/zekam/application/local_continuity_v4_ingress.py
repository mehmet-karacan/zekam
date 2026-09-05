"""Authority-free contracts for dormant Codex 0.151 operational-v4 ingress."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import (
    ContinuityBinding,
    LocalContext,
    digest_text,
    logical,
    timestamp,
    uuid_text,
)
from zekam.application.local_continuity_v4_writer import CurrentSourceSnapshot
from zekam.application.local_hook_command_contract import ReviewedHookCommand
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

SESSION_START_CONTEXT_SCHEMA = "zekam-codex-session-start-context/v1"
STARTUP_CONTEXT_BUDGET_PROFILE = "utf8-bytes-minimum-one/v1"
MAX_STARTUP_FRAGMENT_BUDGET_UNITS = 2_048
MAX_ADDITIONAL_CONTEXT_UTF8_BYTES = 16_384
MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES = 32_847


def startup_fragment_budget_units(text: str) -> int:
    if type(text) is not str:
        raise ValidationFailed("V4 ingress exact fragment text required")
    return count_context_tokens(text)


def _session_start_success_stdout(additional_context: str) -> bytes:
    if type(additional_context) is not str:
        raise ValidationFailed("V4 ingress exact additional context string required")
    try:
        result = (
            canonical_json(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": additional_context,
                    }
                }
            ).encode("utf-8")
            + b"\n"
        )
    except UnicodeEncodeError as exc:
        raise ValidationFailed("V4 ingress success stdout UTF-8 required") from exc
    if not 1 <= len(result) <= MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES:
        raise ValidationFailed("V4 ingress success stdout outside byte bound")
    return result


def _whole_second(value: object) -> str:
    if type(value) is not str:
        raise ValidationFailed("V4 ingress exact timestamp required")
    checked = timestamp(value)
    if len(checked) != 25 or not checked.endswith("+00:00") or "." in checked:
        raise ValidationFailed("V4 ingress whole-second UTC timestamp required")
    return checked


def _exact_digest(value: object) -> str:
    if type(value) is not str:
        raise ValidationFailed("V4 ingress exact digest string required")
    digest_text(value)
    return value


@dataclass(frozen=True, slots=True, init=False)
class ManagedProcessSnapshot:
    attachment_id: str
    captured_at: str
    native_pid: int
    native_uid: int
    native_start_token: str
    native_artifact_digest: str
    client_contract_digest: str
    hook_set_digest: str
    ancestry_policy_digest: str
    reviewed_commands: tuple[ReviewedHookCommand, ...]

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("V4 ingress process snapshots are concrete-adapter owned")

    def __post_init__(self) -> None:
        if type(self.attachment_id) is not str:
            raise ValidationFailed("V4 ingress exact attachment UUID required")
        uuid_text(self.attachment_id, "attachment")
        _whole_second(self.captured_at)
        if type(self.native_pid) is not int or self.native_pid < 1:
            raise ValidationFailed("V4 ingress native PID invalid")
        if type(self.native_uid) is not int or self.native_uid < 0:
            raise ValidationFailed("V4 ingress native UID invalid")
        logical(self.native_start_token, "native start token")
        for value in (
            self.native_artifact_digest,
            self.client_contract_digest,
            self.hook_set_digest,
            self.ancestry_policy_digest,
        ):
            _exact_digest(value)
        if (
            type(self.reviewed_commands) is not tuple
            or len(self.reviewed_commands) != 3
            or tuple(command.external_event_type for command in self.reviewed_commands)
            != ("SessionStart", "PreCompact", "PostCompact")
            or any(type(command) is not ReviewedHookCommand for command in self.reviewed_commands)
        ):
            raise ValidationFailed("V4 ingress exact reviewed command tuple required")
        for command in self.reviewed_commands:
            command.__post_init__()
            if (
                command.attachment_id != self.attachment_id
                or command.client_contract_digest != self.client_contract_digest
                or command.hook_set_digest != self.hook_set_digest
            ):
                raise PolicyViolation("V4 ingress reviewed command scope drift")


@dataclass(frozen=True, slots=True, init=False)
class ManagedInvocationSnapshot:
    delivery_id: str
    observed_at: str
    process_generation_digest: str
    ancestry_policy_digest: str
    native_pid: int
    native_uid: int
    native_start_token: str
    native_artifact_digest: str
    hook_pid: int
    hook_uid: int
    hook_start_token: str
    shell_artifact_digest: str
    python_launcher_artifact_digest: str
    python_runtime_artifact_digest: str
    launch_command_digest: str
    observation_digest: str
    spool_digest: str

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("V4 ingress invocation snapshots are concrete-adapter owned")

    def __post_init__(self) -> None:
        _exact_digest(self.delivery_id)
        _whole_second(self.observed_at)
        for value in (
            self.process_generation_digest,
            self.ancestry_policy_digest,
            self.native_artifact_digest,
            self.shell_artifact_digest,
            self.python_launcher_artifact_digest,
            self.python_runtime_artifact_digest,
            self.launch_command_digest,
            self.observation_digest,
            self.spool_digest,
        ):
            _exact_digest(value)
        for integer_value, minimum in (
            (self.native_pid, 1),
            (self.native_uid, 0),
            (self.hook_pid, 1),
            (self.hook_uid, 0),
        ):
            if type(integer_value) is not int or integer_value < minimum:
                raise ValidationFailed("V4 ingress invocation process integer invalid")
        logical(self.native_start_token, "native start token")
        logical(self.hook_start_token, "hook start token")
        if self.native_pid == self.hook_pid or self.native_uid != self.hook_uid:
            raise PolicyViolation("V4 ingress invocation ancestry tuple invalid")
        if (
            len(
                {
                    self.native_artifact_digest,
                    self.shell_artifact_digest,
                    self.python_launcher_artifact_digest,
                    self.python_runtime_artifact_digest,
                }
            )
            != 4
        ):
            raise PolicyViolation("V4 ingress four distinct artifacts required")


@dataclass(frozen=True, slots=True, init=False)
class FrozenCurrentStartupContext:
    binding: ContinuityBinding
    binding_digest: str
    source_snapshot: CurrentSourceSnapshot
    environment_evidence_digest: str
    context: LocalContext
    manifest_body_json: str
    manifest_digest: str
    hydration_key: str
    hydration_body_json: str
    hydration_receipt_digest: str
    observed_at: str
    additional_context: str
    output_digest: str
    success_stdout: bytes

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("V4 ingress frozen contexts are concrete-source owned")

    def __post_init__(self) -> None:
        if type(self.binding) is not ContinuityBinding:
            raise ValidationFailed("V4 ingress exact context binding required")
        self.binding.__post_init__()
        for value in (
            self.binding_digest,
            self.environment_evidence_digest,
            self.manifest_digest,
            self.hydration_receipt_digest,
            self.output_digest,
        ):
            _exact_digest(value)
        if type(self.source_snapshot) is not CurrentSourceSnapshot:
            raise ValidationFailed("V4 ingress exact source snapshot required")
        self.source_snapshot.__post_init__()
        if type(self.context) is not LocalContext:
            raise ValidationFailed("V4 ingress exact LocalContext required")
        self.context.__post_init__()
        self.context.assert_scope(self.binding)
        logical(self.hydration_key, "V4 ingress hydration key")
        _whole_second(self.observed_at)
        for value, label in (
            (self.manifest_body_json, "manifest body"),
            (self.hydration_body_json, "hydration body"),
            (self.additional_context, "additional context"),
        ):
            if type(value) is not str:
                raise ValidationFailed(f"V4 ingress exact {label} string required")
        if type(self.success_stdout) is not bytes or not self.success_stdout.endswith(b"\n"):
            raise ValidationFailed("V4 ingress exact success stdout bytes required")
        if (
            self.binding_digest != self.binding.binding_digest
            or self.source_snapshot.source_snapshot_id != self.binding.source_snapshot_id
        ):
            raise PolicyViolation("V4 ingress frozen context binding scope drift")
        manifest_body = json.loads(
            canonical_json(
                {
                    "binding_digest": self.binding.binding_digest,
                    "session_id": self.binding.session_id,
                    "checkpoint_digest": None,
                    "context": self.context.body(),
                }
            )
        )
        hydration_body = {
            "session_id": self.binding.session_id,
            "manifest_digest": self.manifest_digest,
            "idempotency_key": self.hydration_key,
            "grants_authority": False,
        }
        expected_additional = _render_additional_context(
            self.context, self.source_snapshot, self.manifest_digest
        )
        expected_stdout = _session_start_success_stdout(expected_additional)
        try:
            manifest_document = json.loads(self.manifest_body_json)
            hydration_document = json.loads(self.hydration_body_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationFailed("V4 ingress frozen context canonical JSON required") from exc
        if (
            manifest_document != manifest_body
            or canonical_json(manifest_document) != self.manifest_body_json
            or self.manifest_digest != digest(manifest_body)
            or hydration_document != hydration_body
            or canonical_json(hydration_document) != self.hydration_body_json
            or self.hydration_receipt_digest != digest(hydration_body)
            or self.additional_context != expected_additional
            or self.output_digest != digest(expected_additional)
            or self.success_stdout != expected_stdout
        ):
            raise PolicyViolation("V4 ingress frozen context canonical parity drift")


def _render_additional_context(
    context: LocalContext,
    source_snapshot: CurrentSourceSnapshot,
    manifest_digest: str,
) -> str:
    fragments = dict(context.fragments)
    provenance = {candidate.candidate_id: candidate for candidate in context.selected_provenance}
    rendered: list[dict[str, Any]] = []
    if len(source_snapshot.revision_ref) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in source_snapshot.revision_ref
    ):
        raise PolicyViolation("V4 ingress source revision is not an accepted Git object id")
    for item in context.manifest.selected:
        candidate = provenance[item.candidate_id]
        text = fragments[item.candidate_id]
        source_ref = PurePosixPath(item.source_ref)
        if (
            not item.source_ref
            or item.source_ref.startswith("/")
            or "\\" in item.source_ref
            or "\x00" in item.source_ref
            or source_ref.is_absolute()
            or any(part in {"", ".", ".."} for part in item.source_ref.split("/"))
        ):
            raise PolicyViolation("V4 ingress source reference is not portable")
        if candidate.candidate_digest != item.candidate_digest:
            raise PolicyViolation("V4 ingress additional context provenance drift")
        rendered.append(
            {
                "candidate_id": item.candidate_id,
                "kind": item.kind,
                "source_ref": item.source_ref,
                "content_digest": item.content_digest,
                "token_count": item.token_count,
                "text": text,
            }
        )
    additional = canonical_json(
        {
            "schema": SESSION_START_CONTEXT_SCHEMA,
            "manifest_digest": manifest_digest,
            "source_snapshot_id": source_snapshot.source_snapshot_id,
            "source_revision": source_snapshot.revision_ref,
            "fragments": rendered,
            "provider_called": False,
            "model_summary": False,
            "grants_authority": False,
        }
    )
    encoded = additional.encode("utf-8")
    if not 1 <= len(encoded) <= MAX_ADDITIONAL_CONTEXT_UTF8_BYTES:
        raise ValidationFailed("V4 ingress additional context outside bounded output")
    if scan_text(additional, relative_path="continuity/session-start", rules=SECRET_RULES):
        raise PolicyViolation("V4 ingress rendered output secret rejected")
    return additional


def _validate_current_context_inputs(
    *,
    binding: ContinuityBinding,
    context: LocalContext,
    source_snapshot: CurrentSourceSnapshot,
    environment_evidence_digest: str,
    hydration_key: str,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any], str, bytes]:
    if type(binding) is not ContinuityBinding or type(context) is not LocalContext:
        raise ValidationFailed("V4 ingress exact binding/context required")
    binding.__post_init__()
    context.__post_init__()
    if type(source_snapshot) is not CurrentSourceSnapshot:
        raise ValidationFailed("V4 ingress exact source snapshot required")
    source_snapshot.__post_init__()
    if source_snapshot.source_snapshot_id != binding.source_snapshot_id:
        raise PolicyViolation("V4 ingress source snapshot scope drift")
    _exact_digest(environment_evidence_digest)
    logical(hydration_key, "V4 ingress hydration key")
    _whole_second(observed_at)
    selected = context.manifest.selected
    fragments = dict(context.fragments)
    provenance = {candidate.candidate_id: candidate for candidate in context.selected_provenance}
    if tuple(fragments) != tuple(item.candidate_id for item in selected):
        raise PolicyViolation("V4 ingress fragment order differs from compiler selection")
    if sum(item.token_count for item in selected) > MAX_STARTUP_FRAGMENT_BUDGET_UNITS:
        raise ValidationFailed("V4 ingress source context token bound exceeded")
    for item in selected:
        candidate = provenance[item.candidate_id]
        text = fragments[item.candidate_id]
        if (
            digest(text) != item.content_digest
            or startup_fragment_budget_units(text) != item.token_count
            or candidate.candidate_digest != item.candidate_digest
        ):
            raise PolicyViolation("V4 ingress rendered provenance drift")
        if scan_text(text, relative_path="continuity/session-start", rules=SECRET_RULES):
            raise PolicyViolation("V4 ingress context secret rejected")
    manifest_body = {
        "binding_digest": binding.binding_digest,
        "session_id": binding.session_id,
        "checkpoint_digest": None,
        "context": context.body(),
    }
    manifest_digest = digest(manifest_body)
    hydration_body = {
        "session_id": binding.session_id,
        "manifest_digest": manifest_digest,
        "idempotency_key": hydration_key,
        "grants_authority": False,
    }
    additional = _render_additional_context(context, source_snapshot, manifest_digest)
    try:
        encoded = additional.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed("V4 ingress additional context UTF-8 required") from exc
    if not 1 <= len(encoded) <= MAX_ADDITIONAL_CONTEXT_UTF8_BYTES:
        raise ValidationFailed("V4 ingress additional context outside bounded output")
    if scan_text(additional, relative_path="continuity/session-start", rules=SECRET_RULES):
        raise PolicyViolation("V4 ingress rendered output secret rejected")
    success_stdout = _session_start_success_stdout(additional)
    return manifest_body, hydration_body, additional, success_stdout


@dataclass(frozen=True, slots=True)
class SessionStartIngressResult:
    stdout: bytes
    manifest_digest: str | None
    hydration_receipt_digest: str | None
    event_digest: str | None
    attachment_revision_digest: str | None
    replay: bool
    recovery_required: bool

    def __post_init__(self) -> None:
        if type(self.stdout) is not bytes or not self.stdout.endswith(b"\n"):
            raise ValidationFailed("V4 ingress exact stdout bytes required")
        for value in (
            self.manifest_digest,
            self.hydration_receipt_digest,
            self.event_digest,
            self.attachment_revision_digest,
        ):
            if value is not None:
                _exact_digest(value)
        if type(self.replay) is not bool or type(self.recovery_required) is not bool:
            raise ValidationFailed("V4 ingress exact result flags required")


class TrustedProcessManagerPort(Protocol):
    def capture_process(self, binding: ContinuityBinding) -> ManagedProcessSnapshot: ...

    def capture_invocation(
        self,
        binding: ContinuityBinding,
        observation: dict[str, Any],
        spool_digest: str,
        observed_at: str,
        expected_process_generation_digest: str,
        expected_generation_created_at: str,
        expected_managed_receipt_digest: str,
        expected_launch_command: ReviewedHookCommand,
        expected_ancestry_policy_digest: str,
    ) -> ManagedInvocationSnapshot: ...

    def assert_process(self, snapshot: ManagedProcessSnapshot) -> None: ...

    def assert_invocation(self, snapshot: ManagedInvocationSnapshot) -> None: ...

    def recovery_time(self) -> str: ...


class CurrentSessionStartContextPort(Protocol):
    def build(
        self,
        binding: ContinuityBinding,
        *,
        hydration_key: str,
        observed_at: str,
    ) -> FrozenCurrentStartupContext: ...

    def assert_current(
        self, binding: ContinuityBinding, snapshot: FrozenCurrentStartupContext
    ) -> None: ...
