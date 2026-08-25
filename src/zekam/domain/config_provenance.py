"""Field-level configuration provenance and named permission profiles."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4, uuid5

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ManagedRequirementMode(StrEnum):
    DENY = "deny"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class ManagedFieldRequirement:
    field_path: str
    mode: ManagedRequirementMode
    required_value_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.field_path.strip():
            raise ValidationFailed("Managed config field path bos olamaz")
        if self.mode is ManagedRequirementMode.EXACT:
            if self.required_value_digest is None:
                raise ValidationFailed("Managed exact requirement digest ister")
            parse_digest(self.required_value_digest)
        elif self.required_value_digest is not None:
            raise ValidationFailed("Managed deny requirement value digest tasiyamaz")


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    name: str
    precedence: int
    values: dict[str, Any]
    managed: bool = False
    requirements: tuple[ManagedFieldRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or self.precedence < 0:
            raise ValidationFailed("Config layer name/precedence gecersiz")
        if self.requirements and not self.managed:
            raise PolicyViolation("Yalniz managed layer requirement tanimlayabilir")
        paths = tuple(item.field_path for item in self.requirements)
        if len(set(paths)) != len(paths):
            raise ValidationFailed("Config layer duplicate managed requirement iceremez")


@dataclass(frozen=True, slots=True)
class ConfigFieldCandidate:
    layer: str
    value_digest: str
    selected: bool
    disabled_reason: str | None

    def __post_init__(self) -> None:
        parse_digest(self.value_digest)
        if self.selected is (self.disabled_reason is not None):
            raise ValidationFailed("Config field candidate selection/reason tutarsiz")


@dataclass(frozen=True, slots=True)
class ConfigFieldDecision:
    field_path: str
    origin: str
    value: Any
    value_digest: str
    candidates: tuple[ConfigFieldCandidate, ...]
    managed_requirement: ManagedFieldRequirement | None = None

    def __post_init__(self) -> None:
        if not self.field_path.strip() or not self.origin.strip():
            raise ValidationFailed("Config field decision identity bos olamaz")
        parse_digest(self.value_digest)
        if self.value_digest != digest(self.value):
            raise PolicyViolation("Config field effective value digest mismatch")
        selected = tuple(item for item in self.candidates if item.selected)
        if len(selected) != 1 or selected[0].layer != self.origin:
            raise ValidationFailed("Config field exact bir selected origin ister")

    def body(self) -> dict[str, Any]:
        return {
            "field_path": self.field_path,
            "origin": self.origin,
            "value": self.value,
            "value_digest": self.value_digest,
            "candidates": [
                {
                    "layer": item.layer,
                    "value_digest": item.value_digest,
                    "selected": item.selected,
                    "disabled_reason": item.disabled_reason,
                }
                for item in self.candidates
            ],
            "managed_requirement": (
                None
                if self.managed_requirement is None
                else {
                    "field_path": self.managed_requirement.field_path,
                    "mode": self.managed_requirement.mode.value,
                    "required_value_digest": self.managed_requirement.required_value_digest,
                }
            ),
        }


@dataclass(frozen=True, slots=True)
class ConfigProvenanceGraph:
    layer_stack: tuple[str, ...]
    fields: tuple[ConfigFieldDecision, ...]
    effective_document: dict[str, Any]
    effective_digest: str
    graph_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.layer_stack or len(set(self.layer_stack)) != len(self.layer_stack):
            raise ValidationFailed("Config provenance exact layer stack ister")
        if tuple(item.field_path for item in self.fields) != tuple(
            sorted(item.field_path for item in self.fields)
        ):
            raise ValidationFailed("Config provenance fields canonical sirali olmali")
        if self.grants_authority:
            raise PolicyViolation("Config provenance authority uretemez")
        if self.effective_digest != digest(self.effective_document):
            raise PolicyViolation("Config effective document digest mismatch")
        parse_digest(self.graph_digest)
        if self.graph_digest != digest(self.body()):
            raise PolicyViolation("Config provenance graph digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-config-provenance-graph/v1",
            "layer_stack": list(self.layer_stack),
            "fields": [field.body() for field in self.fields],
            "effective_digest": self.effective_digest,
            "grants_authority": False,
        }

    def explain(self, field_path: str) -> ConfigFieldDecision:
        matches = tuple(item for item in self.fields if item.field_path == field_path)
        if len(matches) != 1:
            raise ValidationFailed("Config field provenance bulunamadi")
        return matches[0]


def _flatten(document: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in document.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(_flatten(value, path))
        else:
            result[path] = value
    return result


def _inflate(values: dict[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for path, value in sorted(values.items()):
        cursor = document
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return document


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def compile_config_provenance(layers: tuple[ConfigLayer, ...]) -> ConfigProvenanceGraph:
    if not layers:
        raise ValidationFailed("Config provenance en az bir layer ister")
    ordered = tuple(sorted(layers, key=lambda item: (item.precedence, item.name)))
    if len({item.precedence for item in ordered}) != len(ordered):
        raise ValidationFailed("Config layer precedence unique olmali")
    flattened = {layer.name: _flatten(layer.values) for layer in ordered}
    requirements: dict[str, ManagedFieldRequirement] = {}
    for layer in ordered:
        for requirement in layer.requirements:
            requirements[requirement.field_path] = requirement
    merged_document: dict[str, Any] = {}
    for layer in ordered:
        merged_document = _merge(merged_document, layer.values)
    all_paths = sorted(_flatten(merged_document))
    missing_requirements = sorted(set(requirements) - set(all_paths))
    if missing_requirements:
        raise PolicyViolation(
            "Managed config requirement alani eksik: " + ", ".join(missing_requirements)
        )
    decisions: list[ConfigFieldDecision] = []
    effective: dict[str, Any] = {}
    for path in all_paths:
        candidates = tuple(layer for layer in ordered if path in flattened[layer.name])
        selected_layer = candidates[-1]
        selected_value = flattened[selected_layer.name][path]
        active_requirement = requirements.get(path)
        if active_requirement is not None:
            if (
                active_requirement.mode is ManagedRequirementMode.DENY
                and selected_value is not False
            ):
                raise PolicyViolation(f"Managed deny session override reddedildi: {path}")
            if (
                active_requirement.mode is ManagedRequirementMode.EXACT
                and digest(selected_value) != active_requirement.required_value_digest
            ):
                raise PolicyViolation(f"Managed exact config requirement drift: {path}")
        effective[path] = selected_value
        candidate_rows = tuple(
            ConfigFieldCandidate(
                layer.name,
                digest(flattened[layer.name][path]),
                layer is selected_layer,
                None if layer is selected_layer else "higher-precedence-layer",
            )
            for layer in candidates
        )
        decisions.append(
            ConfigFieldDecision(
                path,
                selected_layer.name,
                selected_value,
                digest(selected_value),
                candidate_rows,
                active_requirement,
            )
        )
    effective_document = _inflate(effective)
    effective_digest = digest(effective_document)
    draft = {
        "schema": "zekam-config-provenance-graph/v1",
        "layer_stack": [layer.name for layer in ordered],
        "fields": [field.body() for field in decisions],
        "effective_digest": effective_digest,
        "grants_authority": False,
    }
    return ConfigProvenanceGraph(
        tuple(layer.name for layer in ordered),
        tuple(decisions),
        effective_document,
        effective_digest,
        digest(draft),
    )


@dataclass(frozen=True, slots=True)
class PermissionProfileRevision:
    id: UUID
    realm_id: UUID | None
    name: str
    revision: int
    allowed_capabilities: tuple[str, ...]
    denied_capabilities: tuple[str, ...]
    managed: bool
    created_at: dt.datetime
    profile_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip() or self.revision < 1 or self.created_at.tzinfo is None:
            raise ValidationFailed("Permission profile identity/revision/time gecersiz")
        if self.allowed_capabilities != tuple(sorted(set(self.allowed_capabilities))):
            raise ValidationFailed("Permission profile allowed set canonical olmali")
        if self.denied_capabilities != tuple(sorted(set(self.denied_capabilities))):
            raise ValidationFailed("Permission profile denied set canonical olmali")
        if set(self.allowed_capabilities) & set(self.denied_capabilities):
            raise ValidationFailed("Permission profile allow/deny cakismasi")
        if any(not value.strip() for value in self.allowed_capabilities + self.denied_capabilities):
            raise ValidationFailed("Permission profile capability bos olamaz")
        if self.grants_authority:
            raise PolicyViolation("Permission profile authority veremez")
        if self.profile_digest:
            parse_digest(self.profile_digest)
            if self.profile_digest != digest(self.body()):
                raise PolicyViolation("Permission profile digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-permission-profile-revision/v1",
            "id": str(self.id),
            "realm_id": None if self.realm_id is None else str(self.realm_id),
            "name": self.name,
            "revision": self.revision,
            "allowed_capabilities": list(self.allowed_capabilities),
            "denied_capabilities": list(self.denied_capabilities),
            "managed": self.managed,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @classmethod
    def create(cls, **values: Any) -> PermissionProfileRevision:
        values.setdefault("id", uuid4())
        values["allowed_capabilities"] = tuple(sorted(set(values["allowed_capabilities"])))
        values["denied_capabilities"] = tuple(sorted(set(values["denied_capabilities"])))
        values["profile_digest"] = ""
        draft = cls(**values)
        return replace(draft, profile_digest=digest(draft.body()))

    @classmethod
    def from_flags(
        cls,
        *,
        permission_flags: dict[str, bool],
        **values: Any,
    ) -> PermissionProfileRevision:
        """Raw boolean permission'lari kayip olmadan named revision'a derler."""
        expected = {
            "filesystem.read",
            "filesystem.write",
            "network.access",
            "process.run",
        }
        if set(permission_flags) != expected or any(
            type(enabled) is not bool for enabled in permission_flags.values()
        ):
            raise ValidationFailed("Permission flag set exact boolean capability ister")
        values["allowed_capabilities"] = tuple(
            capability for capability, enabled in permission_flags.items() if enabled
        )
        values["denied_capabilities"] = tuple(
            capability for capability, enabled in permission_flags.items() if not enabled
        )
        return cls.create(**values)

    def resolve_session(
        self,
        requested_capabilities: tuple[str, ...],
    ) -> tuple[str, ...]:
        requested = tuple(sorted(set(requested_capabilities)))
        denied = set(requested) & set(self.denied_capabilities)
        outside = set(requested) - set(self.allowed_capabilities)
        if denied or outside:
            reason = "managed deny" if self.managed and denied else "profile capability scope"
            raise PolicyViolation(f"Permission session override reddedildi: {reason}")
        return requested


def builtin_permission_profiles(
    now: dt.datetime | None = None,
) -> tuple[PermissionProfileRevision, ...]:
    moment = now or dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    namespace = UUID("1e411e57-30d5-4ce5-a1a9-668f8387ecdd")
    return (
        PermissionProfileRevision.from_flags(
            id=uuid5(namespace, "read-only@1"),
            realm_id=None,
            name="read-only",
            revision=1,
            permission_flags={
                "filesystem.read": True,
                "filesystem.write": False,
                "network.access": False,
                "process.run": False,
            },
            managed=True,
            created_at=moment,
        ),
        PermissionProfileRevision.from_flags(
            id=uuid5(namespace, "workspace-write-no-network@1"),
            realm_id=None,
            name="workspace-write-no-network",
            revision=1,
            permission_flags={
                "filesystem.read": True,
                "filesystem.write": True,
                "network.access": False,
                "process.run": True,
            },
            managed=True,
            created_at=moment,
        ),
    )
