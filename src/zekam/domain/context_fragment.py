"""Typed, authority-free model context fragments and payload bindings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_continuity import AuthorityLevel, _safe_logical
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ContextContentKind(StrEnum):
    SYSTEM_INSTRUCTION = "system-instruction"
    USER_MESSAGE = "user-message"
    ASSISTANT_MESSAGE = "assistant-message"
    TOOL_RESULT = "tool-result"
    WORK_CONTEXT = "work-context"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    CHECKPOINT = "checkpoint"


class ContextRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ContextVisibility(StrEnum):
    MODEL = "model-visible"
    CLIENT = "client-visible"
    RUNTIME = "runtime-only"
    DIAGNOSTIC = "diagnostic-only"


@dataclass(frozen=True, slots=True)
class ContextFragment:
    fragment_id: str
    candidate_id: str
    content_kind: ContextContentKind
    role: ContextRole
    order: int
    visibility: ContextVisibility
    authority: AuthorityLevel
    source_ref: str
    source_revision: str
    content_digest: str
    token_count: int
    required: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.content_kind, ContextContentKind):
            raise ValidationFailed("Context fragment content kind registry disinda")
        if not isinstance(self.role, ContextRole):
            raise ValidationFailed("Context fragment role registry disinda")
        if not isinstance(self.visibility, ContextVisibility):
            raise ValidationFailed("Context fragment visibility registry disinda")
        if not isinstance(self.authority, AuthorityLevel):
            raise ValidationFailed("Context fragment authority seviyesi gecersiz")
        for value, label in (
            (self.fragment_id, "Fragment"),
            (self.candidate_id, "Fragment candidate"),
            (self.source_ref, "Fragment source"),
            (self.source_revision, "Fragment source revision"),
        ):
            _safe_logical(value, label)
        parse_digest(self.content_digest)
        if self.order < 0 or self.token_count < 1:
            raise ValidationFailed("Fragment order sifir veya pozitif, token sayisi pozitif olmali")
        if self.grants_authority:
            raise PolicyViolation("Context fragment authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "candidate_id": self.candidate_id,
            "content_kind": self.content_kind.value,
            "role": self.role.value,
            "order": self.order,
            "visibility": self.visibility.value,
            "authority": int(self.authority),
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "content_digest": self.content_digest,
            "token_count": self.token_count,
            "required": self.required,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class ContextFragmentSet:
    context_manifest_digest: str
    fragments: tuple[ContextFragment, ...]

    def __post_init__(self) -> None:
        parse_digest(self.context_manifest_digest)
        if not self.fragments:
            raise ValidationFailed("Context fragment set bos olamaz")
        if len({item.fragment_id for item in self.fragments}) != len(self.fragments):
            raise ValidationFailed("Context fragment kimlikleri tekil olmali")
        if len({item.candidate_id for item in self.fragments}) != len(self.fragments):
            raise ValidationFailed("Selected candidate exact bir fragment uretmeli")
        ordered = tuple(sorted(self.fragments, key=lambda item: item.order))
        if ordered != self.fragments or tuple(item.order for item in ordered) != tuple(
            range(len(ordered))
        ):
            raise ValidationFailed("Context fragment sirasi exact ve bitisik olmali")

    @property
    def fragment_set_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-fragment-set/v2",
            "context_manifest_digest": self.context_manifest_digest,
            "fragments": [item.body() for item in self.fragments],
        }


@dataclass(frozen=True, slots=True)
class ModelVisiblePayloadBinding:
    context_manifest_digest: str
    fragment_set_digest: str
    ordered_model_fragment_ids: tuple[str, ...]
    request_payload_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.context_manifest_digest)
        parse_digest(self.fragment_set_digest)
        parse_digest(self.request_payload_digest)
        if not self.ordered_model_fragment_ids or len(set(self.ordered_model_fragment_ids)) != len(
            self.ordered_model_fragment_ids
        ):
            raise ValidationFailed("Model-visible fragment kimlikleri bos olamaz ve tekil olmali")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-visible-payload-binding/v1",
            "context_manifest_digest": self.context_manifest_digest,
            "fragment_set_digest": self.fragment_set_digest,
            "ordered_model_fragment_ids": list(self.ordered_model_fragment_ids),
            "request_payload_digest": self.request_payload_digest,
        }
