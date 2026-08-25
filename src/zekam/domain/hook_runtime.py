"""Typed, authority-free lifecycle hook contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7


class HookEventType(StrEnum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    USER_INPUT_SUBMITTED = "user.input.submitted"
    TURN_START = "turn.start"
    TURN_STOP = "turn.stop"
    PRE_TOOL = "pre.tool"
    POST_TOOL = "post.tool"
    PERMISSION_REQUEST = "permission.request"
    PRE_COMPACT = "pre.compact"
    POST_COMPACT = "post.compact"
    CHECKPOINT_CREATED = "checkpoint.created"
    AGENT_SPAWNED = "agent.spawned"
    AGENT_COMPLETED = "agent.completed"
    RECOVERY_REQUIRED = "recovery.required"


class HookExecutionMode(StrEnum):
    COMMAND = "command"
    PYTHON = "python"
    MCP = "mcp"
    INTERNAL = "internal"


class HookFailurePolicy(StrEnum):
    ABORT = "abort"
    WARN = "warn"
    QUARANTINE = "quarantine"


class HookResultKind(StrEnum):
    PROPOSAL = "proposal"
    DENY = "deny"
    OBSERVATION = "observation"


class HookLoadState(StrEnum):
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"


def _text(value: str, field: str, maximum: int = 255) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValidationFailed(f"{field} bos olamaz ve {maximum} karakteri gecemez")
    return normalized


def _schema(document: dict[str, Any], field: str) -> dict[str, Any]:
    try:
        Draft202012Validator.check_schema(document)
    except SchemaError as exc:
        raise ValidationFailed(f"{field} JSON Schema gecersiz") from exc
    return document


def validate_payload(schema: dict[str, Any], payload: Any, field: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        raise ValidationFailed(f"{field} typed schema ile uyusmuyor") from exc


@dataclass(frozen=True, slots=True)
class HookSpecRevision:
    id: UUID
    realm_id: UUID
    hook_id: str
    revision: int
    event_type: HookEventType
    required: bool
    source_layer: str
    timeout_ms: int
    execution_mode: HookExecutionMode
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_schema_digest: str
    output_schema_digest: str
    permission_profile_name: str
    permission_profile_digest: str
    failure_policy: HookFailurePolicy
    created_at: dt.datetime
    hook_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        hook_id: str,
        revision: int,
        event_type: HookEventType,
        required: bool,
        source_layer: str,
        timeout_ms: int,
        execution_mode: HookExecutionMode,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        permission_profile_name: str,
        permission_profile_digest: str,
        failure_policy: HookFailurePolicy,
        created_at: dt.datetime,
        id: UUID | None = None,
    ) -> HookSpecRevision:
        checked_input = _schema(input_schema, "hook input schema")
        checked_output = _schema(output_schema, "hook output schema")
        item = cls(
            id or new_uuid7(),
            realm_id,
            _text(hook_id, "hook_id"),
            revision,
            event_type,
            required,
            _text(source_layer, "source_layer"),
            timeout_ms,
            execution_mode,
            checked_input,
            checked_output,
            digest(checked_input),
            digest(checked_output),
            _text(permission_profile_name, "permission_profile_name"),
            permission_profile_digest,
            failure_policy,
            created_at,
            "",
            False,
        )
        item._validate()
        return replace(item, hook_digest=digest(item.body()))

    def _validate(self) -> None:
        if self.revision < 1 or self.timeout_ms < 1 or self.timeout_ms > 300_000:
            raise ValidationFailed("Hook revision/timeout gecersiz")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Hook created_at timezone-aware olmali")
        if self.required and self.failure_policy is HookFailurePolicy.WARN:
            raise PolicyViolation("Required hook warning ile fail-open olamaz")
        parse_digest(self.permission_profile_digest)
        if self.grants_authority:
            raise PolicyViolation("Hook spec authority veremez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-hook-spec-revision/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "hook_id": self.hook_id,
            "revision": self.revision,
            "event_type": self.event_type.value,
            "required": self.required,
            "source_layer": self.source_layer,
            "timeout_ms": self.timeout_ms,
            "execution_mode": self.execution_mode.value,
            "input_schema_digest": self.input_schema_digest,
            "output_schema_digest": self.output_schema_digest,
            "permission_profile_name": self.permission_profile_name,
            "permission_profile_digest": self.permission_profile_digest,
            "failure_policy": self.failure_policy.value,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        if self.input_schema_digest != digest(self.input_schema):
            raise PolicyViolation("Hook input schema digest mismatch")
        if self.output_schema_digest != digest(self.output_schema):
            raise PolicyViolation("Hook output schema digest mismatch")
        if self.hook_digest != digest(self.body()):
            raise PolicyViolation("Hook spec digest mismatch")


@dataclass(frozen=True, slots=True)
class HookConfigurationSnapshot:
    generation: int
    hooks: tuple[HookSpecRevision, ...]
    unavailable_optional: tuple[str, ...]
    required_load_errors: tuple[str, ...]
    snapshot_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        generation: int,
        hooks: tuple[HookSpecRevision, ...],
        unavailable_optional: tuple[str, ...] = (),
        required_load_errors: tuple[str, ...] = (),
    ) -> HookConfigurationSnapshot:
        ordered = tuple(sorted(hooks, key=lambda item: (item.event_type.value, item.hook_id)))
        unavailable = tuple(sorted(set(unavailable_optional)))
        errors = tuple(sorted(set(required_load_errors)))
        if generation < 1:
            raise ValidationFailed("Hook snapshot generation pozitif olmali")
        identities = tuple((item.hook_id, item.revision) for item in ordered)
        if len(set(identities)) != len(identities):
            raise ValidationFailed("Hook snapshot duplicate revision iceremez")
        draft = cls(generation, ordered, unavailable, errors, "", False)
        return replace(draft, snapshot_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-hook-configuration-snapshot/v1",
            "generation": self.generation,
            "hook_digests": [item.hook_digest for item in self.hooks],
            "unavailable_optional": list(self.unavailable_optional),
            "required_load_errors": list(self.required_load_errors),
            "grants_authority": False,
        }

    def assert_session_startable(self) -> None:
        if self.required_load_errors:
            raise PolicyViolation(
                "Required hook yuklenemedi; session baslangici reddedildi: "
                + ", ".join(self.required_load_errors)
            )


@dataclass(frozen=True, slots=True)
class HookRuntimeRevision:
    id: UUID
    realm_id: UUID
    hook_id: str
    hook_revision: int
    adapter_ref: str
    adapter_digest: str
    permission_capabilities: tuple[str, ...]
    load_state: HookLoadState
    captured_at: dt.datetime
    expires_at: dt.datetime
    runtime_digest: str

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        hook_id: str,
        hook_revision: int,
        adapter_ref: str,
        adapter_digest: str,
        permission_capabilities: tuple[str, ...],
        load_state: HookLoadState,
        captured_at: dt.datetime,
        expires_at: dt.datetime,
        id: UUID | None = None,
    ) -> HookRuntimeRevision:
        item = cls(
            id or new_uuid7(),
            realm_id,
            _text(hook_id, "hook_id"),
            hook_revision,
            _text(adapter_ref, "adapter_ref"),
            adapter_digest,
            tuple(
                sorted({_text(value, "permission capability") for value in permission_capabilities})
            ),
            load_state,
            captured_at,
            expires_at,
            "",
        )
        item._validate()
        return replace(item, runtime_digest=digest(item.body()))

    def _validate(self) -> None:
        if self.hook_revision < 1:
            raise ValidationFailed("Hook runtime revision pozitif olmali")
        parse_digest(self.adapter_digest)
        if self.captured_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValidationFailed("Hook runtime zamanlari timezone-aware olmali")
        if self.expires_at <= self.captured_at:
            raise ValidationFailed("Hook runtime expiry captured_at sonrasinda olmali")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-hook-runtime-revision/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "hook_id": self.hook_id,
            "hook_revision": self.hook_revision,
            "adapter_ref": self.adapter_ref,
            "adapter_digest": self.adapter_digest,
            "permission_capabilities": list(self.permission_capabilities),
            "load_state": self.load_state.value,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    def assert_integrity(self) -> None:
        if self.runtime_digest != digest(self.body()):
            raise PolicyViolation("Hook runtime digest mismatch")


@dataclass(frozen=True, slots=True)
class CompiledHookEntry:
    ordinal: int
    spec: HookSpecRevision
    runtime: HookRuntimeRevision | None
    disabled_reason: str | None

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValidationFailed("Compiled hook ordinal pozitif olmali")
        if self.runtime is not None and (
            self.runtime.realm_id != self.spec.realm_id
            or self.runtime.hook_id != self.spec.hook_id
            or self.runtime.hook_revision != self.spec.revision
        ):
            raise PolicyViolation("Hook spec/runtime exact binding mismatch")
        if (self.runtime is None) is not (self.disabled_reason is not None):
            raise ValidationFailed("Compiled hook runtime/disabled reason tutarsiz")

    def body(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "hook_digest": self.spec.hook_digest,
            "runtime_digest": None if self.runtime is None else self.runtime.runtime_digest,
            "disabled_reason": self.disabled_reason,
        }


@dataclass(frozen=True, slots=True)
class CompiledHookSet:
    realm_id: UUID
    generation: int
    config_effective_digest: str
    entries: tuple[CompiledHookEntry, ...]
    required_load_errors: tuple[str, ...]
    hook_set_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        generation: int,
        config_effective_digest: str,
        entries: tuple[CompiledHookEntry, ...],
        required_load_errors: tuple[str, ...] = (),
    ) -> CompiledHookSet:
        parse_digest(config_effective_digest)
        ordered = tuple(sorted(entries, key=lambda item: item.ordinal))
        if generation < 1 or tuple(item.ordinal for item in ordered) != tuple(
            range(1, len(ordered) + 1)
        ):
            raise ValidationFailed("Compiled hook set canonical ordinal ister")
        if any(item.spec.realm_id != realm_id for item in ordered):
            raise PolicyViolation("Compiled hook set cross-realm entry iceremez")
        draft = cls(
            realm_id,
            generation,
            config_effective_digest,
            ordered,
            tuple(sorted(set(required_load_errors))),
            "",
            False,
        )
        return replace(draft, hook_set_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-compiled-hook-set/v1",
            "realm_id": str(self.realm_id),
            "generation": self.generation,
            "config_effective_digest": self.config_effective_digest,
            "entries": [item.body() for item in self.entries],
            "required_load_errors": list(self.required_load_errors),
            "grants_authority": False,
        }

    def assert_session_startable(self) -> None:
        if self.required_load_errors:
            raise PolicyViolation(
                "Required hook yuklenemedi; session baslangici reddedildi: "
                + ", ".join(self.required_load_errors)
            )


@dataclass(frozen=True, slots=True)
class HookPreviewEntry:
    hook_id: str
    hook_revision: int
    event_type: HookEventType
    input_digest: str
    timeout_ms: int
    permission_profile_digest: str
    failure_policy: HookFailurePolicy
    hook_digest: str
    will_execute: bool
    disabled_reason: str | None
    effect_performed: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.effect_performed or self.grants_authority:
            raise PolicyViolation("Hook preview effect veya authority uretemez")


@dataclass(frozen=True, slots=True)
class HookAdapterResult:
    kind: HookResultKind
    payload: Any
    effect_performed: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.effect_performed or self.grants_authority:
            raise PolicyViolation("Hook adapter direct effect veya authority uretemez")


@dataclass(frozen=True, slots=True)
class HookRunOutcome:
    hook_id: str
    hook_revision: int
    kind: HookResultKind | None
    status: str
    input_digest: str
    output_digest: str | None
    proposal_digest: str | None
    warning: str | None
    requires_governed_effect: bool
    effect_performed: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.input_digest)
        if self.output_digest is not None:
            parse_digest(self.output_digest)
        if self.proposal_digest is not None:
            parse_digest(self.proposal_digest)
        if self.effect_performed or self.grants_authority:
            raise PolicyViolation("Hook outcome effect veya authority uretemez")
        if self.requires_governed_effect is not (self.kind is HookResultKind.PROPOSAL):
            raise ValidationFailed("Hook proposal governance bayragi tutarsiz")
