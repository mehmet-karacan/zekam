"""Versioned tool specification, runtime and model-visible set contracts."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7


class ToolExposure(StrEnum):
    DIRECT = "direct"
    DEFERRED_SEARCH = "deferred-search"
    CODE_MODE_ONLY = "code-mode-only"
    HIDDEN_DISPATCH = "hidden-dispatch"


def _text(value: str, field: str, *, maximum: int = 255) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValidationFailed(f"{field} bos olamaz ve {maximum} karakteri gecemez")
    return normalized


def _digest(value: str) -> str:
    parse_digest(value)
    return value


@dataclass(frozen=True, slots=True)
class ToolSpecRevision:
    id: UUID
    realm_id: UUID
    tool_id: str
    revision: int
    name: str
    description: str
    input_schema_digest: str
    output_schema_digest: str
    created_at: dt.datetime
    spec_digest: str

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        tool_id: str,
        revision: int,
        name: str,
        description: str,
        input_schema_digest: str,
        output_schema_digest: str,
        created_at: dt.datetime,
        id: UUID | None = None,
    ) -> ToolSpecRevision:
        item = cls(
            id or new_uuid7(),
            realm_id,
            _text(tool_id, "tool_id"),
            revision,
            _text(name, "name"),
            _text(description, "description", maximum=4000),
            _digest(input_schema_digest),
            _digest(output_schema_digest),
            created_at,
            "",
        )
        item._validate()
        return replace(item, spec_digest=digest(item.body()))

    def _validate(self) -> None:
        if self.revision < 1:
            raise ValidationFailed("Tool spec revision pozitif olmali")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Tool spec zamani timezone-aware olmali")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-tool-spec-revision/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "tool_id": self.tool_id,
            "revision": self.revision,
            "name": self.name,
            "description": self.description,
            "input_schema_digest": self.input_schema_digest,
            "output_schema_digest": self.output_schema_digest,
            "created_at": self.created_at,
        }

    def assert_digest(self) -> None:
        if self.spec_digest != digest(self.body()):
            raise PolicyViolation("Tool spec supplied digest mismatch")


@dataclass(frozen=True, slots=True)
class ToolRuntimeRevision:
    id: UUID
    realm_id: UUID
    tool_id: str
    revision: int
    adapter_ref: str
    executable_revision: str
    executable_digest: str
    permission_capabilities: tuple[str, ...]
    parallel_supported: bool
    captured_at: dt.datetime
    expires_at: dt.datetime
    runtime_digest: str

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        tool_id: str,
        revision: int,
        adapter_ref: str,
        executable_revision: str,
        executable_digest: str,
        permission_capabilities: tuple[str, ...],
        parallel_supported: bool,
        captured_at: dt.datetime,
        expires_at: dt.datetime,
        id: UUID | None = None,
    ) -> ToolRuntimeRevision:
        capabilities = tuple(
            sorted({_text(value, "permission capability") for value in permission_capabilities})
        )
        item = cls(
            id or new_uuid7(),
            realm_id,
            _text(tool_id, "tool_id"),
            revision,
            _text(adapter_ref, "adapter_ref"),
            _text(executable_revision, "executable_revision"),
            _digest(executable_digest),
            capabilities,
            parallel_supported,
            captured_at,
            expires_at,
            "",
        )
        item._validate()
        return replace(item, runtime_digest=digest(item.body()))

    def _validate(self) -> None:
        if self.revision < 1:
            raise ValidationFailed("Tool runtime revision pozitif olmali")
        if self.captured_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValidationFailed("Tool runtime zamanlari timezone-aware olmali")
        if self.expires_at <= self.captured_at:
            raise ValidationFailed("Tool runtime expiry captured_at sonrasinda olmali")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-tool-runtime-revision/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "tool_id": self.tool_id,
            "revision": self.revision,
            "adapter_ref": self.adapter_ref,
            "executable_revision": self.executable_revision,
            "executable_digest": self.executable_digest,
            "permission_capabilities": self.permission_capabilities,
            "parallel_supported": self.parallel_supported,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    def assert_digest(self) -> None:
        if self.runtime_digest != digest(self.body()):
            raise PolicyViolation("Tool runtime supplied digest mismatch")


@dataclass(frozen=True, slots=True)
class ToolSetEntry:
    tool_id: str
    revision: int
    exposure: ToolExposure
    spec_digest: str
    runtime_digest: str

    def __post_init__(self) -> None:
        _text(self.tool_id, "tool_id")
        if self.revision < 1:
            raise ValidationFailed("Tool set revision pozitif olmali")
        _digest(self.spec_digest)
        _digest(self.runtime_digest)

    def body(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "revision": self.revision,
            "exposure": self.exposure.value,
            "spec_digest": self.spec_digest,
            "runtime_digest": self.runtime_digest,
        }


@dataclass(frozen=True, slots=True)
class DeferredToolMatch:
    tool_id: str
    revision: int
    name: str
    description: str
    spec_digest: str
    runtime_digest: str
    permission_capabilities: tuple[str, ...]
    score: int

    def __post_init__(self) -> None:
        _text(self.tool_id, "tool_id")
        _text(self.name, "name")
        _text(self.description, "description", maximum=4000)
        _digest(self.spec_digest)
        _digest(self.runtime_digest)
        if self.revision < 1 or self.score < 1:
            raise ValidationFailed("Deferred tool match revision/score gecersiz")


@dataclass(frozen=True, slots=True)
class ToolDispatchWave:
    ordinal: int
    bindings: tuple[ToolDispatchBinding, ...]

    def __post_init__(self) -> None:
        if self.ordinal < 1 or not self.bindings:
            raise ValidationFailed("Tool dispatch wave ordinal ve uye ister")
        identities = tuple(
            (binding.effect_claim_id, binding.tool_id, binding.input_digest)
            for binding in self.bindings
        )
        if len(set(identities)) != len(identities):
            raise ValidationFailed("Tool dispatch wave duplicate binding iceremez")

    def body(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "bindings": [
                {
                    "effect_claim_id": str(binding.effect_claim_id),
                    "turn_execution_snapshot_digest": binding.turn_execution_snapshot_digest,
                    "tool_set_digest": binding.tool_set_digest,
                    "tool_id": binding.tool_id,
                    "revision": binding.revision,
                    "spec_digest": binding.spec_digest,
                    "runtime_digest": binding.runtime_digest,
                    "input_digest": binding.input_digest,
                }
                for binding in self.bindings
            ],
        }


@dataclass(frozen=True, slots=True)
class ToolDispatchPlan:
    turn_execution_snapshot_digest: str
    tool_set_digest: str
    max_parallelism: int
    waves: tuple[ToolDispatchWave, ...]
    plan_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _digest(self.turn_execution_snapshot_digest)
        _digest(self.tool_set_digest)
        if self.max_parallelism < 1 or not self.waves:
            raise ValidationFailed("Tool dispatch plan parallelism ve wave ister")
        if tuple(wave.ordinal for wave in self.waves) != tuple(range(1, len(self.waves) + 1)):
            raise ValidationFailed("Tool dispatch plan wave sirasi canonical olmali")
        if self.grants_authority:
            raise PolicyViolation("Tool dispatch plan authority uretemez")
        if self.plan_digest:
            _digest(self.plan_digest)
            if self.plan_digest != self.computed_digest:
                raise PolicyViolation("Tool dispatch plan digest mismatch")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-tool-dispatch-plan/v1",
            "turn_execution_snapshot_digest": self.turn_execution_snapshot_digest,
            "tool_set_digest": self.tool_set_digest,
            "max_parallelism": self.max_parallelism,
            "waves": [wave.body() for wave in self.waves],
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: object) -> ToolDispatchPlan:
        draft = cls(**values, plan_digest="")  # type: ignore[arg-type]
        return replace(draft, plan_digest=draft.computed_digest)


@dataclass(frozen=True, slots=True)
class CompiledToolSet:
    id: UUID
    realm_id: UUID
    role: str
    permission_profile_digest: str
    entries: tuple[ToolSetEntry, ...]
    created_at: dt.datetime
    tool_set_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        role: str,
        permission_profile_digest: str,
        entries: tuple[ToolSetEntry, ...],
        created_at: dt.datetime,
        id: UUID | None = None,
    ) -> CompiledToolSet:
        ordered = tuple(sorted(entries, key=lambda entry: entry.tool_id))
        if len({entry.tool_id for entry in ordered}) != len(ordered):
            raise ValidationFailed("Compiled tool set duplicate tool_id iceremez")
        item = cls(
            id or new_uuid7(),
            realm_id,
            _text(role, "role"),
            _digest(permission_profile_digest),
            ordered,
            created_at,
            "",
        )
        item._validate()
        return replace(item, tool_set_digest=digest(item.body()))

    def _validate(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Compiled tool set zamani timezone-aware olmali")
        if self.grants_authority:
            raise PolicyViolation("Compiled tool set authority veremez")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-compiled-tool-set/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "role": self.role,
            "permission_profile_digest": self.permission_profile_digest,
            "entries": [entry.body() for entry in self.entries],
            "created_at": self.created_at,
            "grants_authority": False,
        }

    def assert_digest(self) -> None:
        if self.tool_set_digest != digest(self.body()):
            raise PolicyViolation("Compiled tool set supplied digest mismatch")

    def model_visible_entries(self, *, code_mode: bool = False) -> tuple[ToolSetEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.exposure is ToolExposure.DIRECT
            or (code_mode and entry.exposure is ToolExposure.CODE_MODE_ONLY)
        )

    def compile_model_payload(self, *, code_mode: bool = False) -> ModelToolPayload:
        self.assert_digest()
        entries = tuple(
            {
                "tool_id": entry.tool_id,
                "revision": entry.revision,
                "spec_digest": entry.spec_digest,
            }
            for entry in self.model_visible_entries(code_mode=code_mode)
        )
        return ModelToolPayload(self.tool_set_digest, code_mode, entries)

    def entry(self, tool_id: str) -> ToolSetEntry:
        matches = tuple(entry for entry in self.entries if entry.tool_id == tool_id)
        if len(matches) != 1:
            raise PolicyViolation("Tool compiled set icinde exact bulunamadi")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ToolDispatchBinding:
    effect_claim_id: UUID
    turn_execution_snapshot_digest: str
    tool_set_digest: str
    tool_id: str
    revision: int
    spec_digest: str
    runtime_digest: str
    input_digest: str

    def __post_init__(self) -> None:
        _digest(self.turn_execution_snapshot_digest)
        _digest(self.tool_set_digest)
        _text(self.tool_id, "tool_id")
        if self.revision < 1:
            raise ValidationFailed("Tool dispatch revision pozitif olmali")
        _digest(self.spec_digest)
        _digest(self.runtime_digest)
        _digest(self.input_digest)


_TOOL_PERMIT_TOKEN = object()
_MODEL_TOOL_PAYLOAD_TOKEN = object()
_MODEL_TOOL_PAYLOAD_SEAL_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class ModelToolPayloadBinding:
    tool_set_digest: str
    code_mode: bool
    ordered_tool_ids: tuple[str, ...]
    serialized_tools_digest: str
    request_payload_digest: str
    _token: object
    _seal: bytes

    def _seal_body(self) -> dict[str, object]:
        return {
            "tool_set_digest": self.tool_set_digest,
            "code_mode": self.code_mode,
            "ordered_tool_ids": list(self.ordered_tool_ids),
            "serialized_tools_digest": self.serialized_tools_digest,
            "request_payload_digest": self.request_payload_digest,
        }

    def assert_valid(self) -> None:
        if self._token is not _MODEL_TOOL_PAYLOAD_TOKEN:
            raise PolicyViolation("Model tool payload binding kanonik serializer'dan gelmedi")
        _digest(self.tool_set_digest)
        _digest(self.serialized_tools_digest)
        _digest(self.request_payload_digest)
        if len(set(self.ordered_tool_ids)) != len(self.ordered_tool_ids):
            raise PolicyViolation("Model tool payload duplicate tool iceriyor")
        expected = hmac.digest(
            _MODEL_TOOL_PAYLOAD_SEAL_KEY,
            canonical_bytes(self._seal_body()),
            hashlib.sha256,
        )
        if not hmac.compare_digest(self._seal, expected):
            raise PolicyViolation("Model tool payload binding muhru gecersiz")


@dataclass(frozen=True, slots=True)
class ModelToolPayload:
    tool_set_digest: str
    code_mode: bool
    entries: tuple[dict[str, object], ...]

    @property
    def serialized_tools_digest(self) -> str:
        return digest(list(self.entries))

    def serialize_request(
        self,
        base_payload: Mapping[str, object],
        *,
        tools_field: str = "tools",
    ) -> ModelToolSerializedRequest:
        if not tools_field.strip() or tools_field in base_payload:
            raise PolicyViolation("Provider request tool alani bos veya onceden doldurulmus")
        payload = dict(base_payload)
        payload[tools_field] = [dict(entry) for entry in self.entries]
        request_payload_digest = digest(payload)
        ordered_tool_ids = tuple(str(entry["tool_id"]) for entry in self.entries)
        serialized_tools_digest = self.serialized_tools_digest
        request_payload_digest = _digest(request_payload_digest)
        seal = hmac.digest(
            _MODEL_TOOL_PAYLOAD_SEAL_KEY,
            canonical_bytes(
                {
                    "tool_set_digest": self.tool_set_digest,
                    "code_mode": self.code_mode,
                    "ordered_tool_ids": list(ordered_tool_ids),
                    "serialized_tools_digest": serialized_tools_digest,
                    "request_payload_digest": request_payload_digest,
                }
            ),
            hashlib.sha256,
        )
        binding = ModelToolPayloadBinding(
            tool_set_digest=self.tool_set_digest,
            code_mode=self.code_mode,
            ordered_tool_ids=ordered_tool_ids,
            serialized_tools_digest=serialized_tools_digest,
            request_payload_digest=request_payload_digest,
            _token=_MODEL_TOOL_PAYLOAD_TOKEN,
            _seal=seal,
        )
        binding.assert_valid()
        return ModelToolSerializedRequest(payload, binding)


@dataclass(frozen=True, slots=True)
class ModelToolSerializedRequest:
    payload: dict[str, object]
    binding: ModelToolPayloadBinding

    def assert_unchanged(self) -> None:
        self.binding.assert_valid()
        if digest(self.payload) != self.binding.request_payload_digest:
            raise PolicyViolation("Serialized provider tool request mutation drift")


@dataclass(frozen=True, slots=True)
class ToolExecutionPermit:
    effect_claim_id: UUID
    turn_execution_snapshot_digest: str
    tool_set_digest: str
    tool_id: str
    revision: int
    spec_digest: str
    runtime_digest: str
    input_digest: str
    _token: object

    def assert_for(self, binding: ToolDispatchBinding) -> None:
        if self._token is not _TOOL_PERMIT_TOKEN:
            raise PolicyViolation("Tool execution permit kanonik gate tarafindan verilmedi")
        if (
            self.effect_claim_id,
            self.turn_execution_snapshot_digest,
            self.tool_set_digest,
            self.tool_id,
            self.revision,
            self.spec_digest,
            self.runtime_digest,
            self.input_digest,
        ) != (
            binding.effect_claim_id,
            binding.turn_execution_snapshot_digest,
            binding.tool_set_digest,
            binding.tool_id,
            binding.revision,
            binding.spec_digest,
            binding.runtime_digest,
            binding.input_digest,
        ):
            raise PolicyViolation("Tool execution permit exact dispatch ile eslesmiyor")


def _issue_tool_execution_permit(binding: ToolDispatchBinding) -> ToolExecutionPermit:
    return ToolExecutionPermit(
        binding.effect_claim_id,
        binding.turn_execution_snapshot_digest,
        binding.tool_set_digest,
        binding.tool_id,
        binding.revision,
        binding.spec_digest,
        binding.runtime_digest,
        binding.input_digest,
        _TOOL_PERMIT_TOKEN,
    )


def assert_tool_dispatch_binding(
    binding: ToolDispatchBinding,
    compiled: CompiledToolSet,
    spec: ToolSpecRevision,
    runtime: ToolRuntimeRevision,
    *,
    now: dt.datetime,
) -> None:
    compiled.assert_digest()
    spec.assert_digest()
    runtime.assert_digest()
    if now.tzinfo is None:
        raise PolicyViolation("Tool dispatch zamani timezone-aware olmali")
    if binding.tool_set_digest != compiled.tool_set_digest:
        raise PolicyViolation("Tool dispatch compiled set digest mismatch")
    entry = compiled.entry(binding.tool_id)
    expected = (
        entry.tool_id,
        entry.revision,
        entry.spec_digest,
        entry.runtime_digest,
    )
    supplied = (
        binding.tool_id,
        binding.revision,
        binding.spec_digest,
        binding.runtime_digest,
    )
    if supplied != expected:
        raise PolicyViolation("Tool dispatch entry binding mismatch")
    if (spec.tool_id, spec.revision, spec.spec_digest) != (
        entry.tool_id,
        entry.revision,
        entry.spec_digest,
    ):
        raise PolicyViolation("Model-visible tool spec revision mismatch")
    if (runtime.tool_id, runtime.revision, runtime.runtime_digest) != (
        entry.tool_id,
        entry.revision,
        entry.runtime_digest,
    ):
        raise PolicyViolation("Executable tool runtime revision mismatch")
    if runtime.expires_at <= now:
        raise PolicyViolation("Executable tool runtime snapshot stale")


def tool_entry_map(compiled: CompiledToolSet) -> Mapping[str, ToolSetEntry]:
    return MappingProxyType({entry.tool_id: entry for entry in compiled.entries})
