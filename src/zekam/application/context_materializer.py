"""Materialize selected context into typed fragments and the exact provider payload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.context_continuity import ContextCandidate, ContextManifest
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragment,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
    ModelVisiblePayloadBinding,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed


@dataclass(frozen=True, slots=True)
class FragmentMaterialization:
    candidate_id: str
    content_kind: ContextContentKind
    role: ContextRole
    visibility: ContextVisibility
    source_ref: str
    content: str


def materialize_fragments(
    manifest: ContextManifest,
    candidates: tuple[ContextCandidate, ...],
    materializations: tuple[FragmentMaterialization, ...],
) -> ContextFragmentSet:
    selected_ids = tuple(item.candidate_id for item in manifest.selected)
    candidate_by_id = {item.candidate_id: item for item in candidates}
    materialization_by_id = {item.candidate_id: item for item in materializations}
    if len(candidate_by_id) != len(candidates) or len(materialization_by_id) != len(
        materializations
    ):
        raise ValidationFailed("Context materialization kimlikleri tekil olmali")
    if set(candidate_by_id) != set(selected_ids) or set(materialization_by_id) != set(selected_ids):
        raise PolicyViolation("Selected context exact candidate/materialization partition ister")
    fragments: list[ContextFragment] = []
    for order, selected in enumerate(manifest.selected):
        candidate = candidate_by_id[selected.candidate_id]
        materialization = materialization_by_id[selected.candidate_id]
        if digest(materialization.content) != candidate.content_digest:
            raise PolicyViolation("Context materialized content digest mismatch")
        fragments.append(
            ContextFragment(
                fragment_id=f"fragment/{selected.candidate_id}",
                candidate_id=selected.candidate_id,
                content_kind=materialization.content_kind,
                role=materialization.role,
                order=order,
                visibility=materialization.visibility,
                authority=candidate.authority,
                source_ref=materialization.source_ref,
                source_revision=candidate.source_revision,
                content_digest=candidate.content_digest,
                token_count=candidate.token_count,
                required=candidate.required,
            )
        )
    return ContextFragmentSet(manifest.manifest_digest, tuple(fragments))


def serialize_model_visible_payload(
    fragment_set: ContextFragmentSet,
    contents: Mapping[str, str],
    *,
    base_payload: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ModelVisiblePayloadBinding]:
    payload = dict(base_payload or {})
    if "messages" in payload:
        raise PolicyViolation("Provider base payload model-visible messages tasiyamaz")
    model_fragments = tuple(
        item for item in fragment_set.fragments if item.visibility is ContextVisibility.MODEL
    )
    if not model_fragments:
        raise PolicyViolation("Provider request en az bir model-visible fragment ister")
    if set(contents) != {item.fragment_id for item in model_fragments}:
        raise PolicyViolation("Model-visible contents exact fragment set ile eslesmeli")
    messages: list[dict[str, str]] = []
    for fragment in model_fragments:
        content = contents[fragment.fragment_id]
        if digest(content) != fragment.content_digest:
            raise PolicyViolation("Model-visible content digest mismatch")
        messages.append({"role": fragment.role.value, "content": content})
    payload["messages"] = messages
    request_digest = digest(payload)
    binding = ModelVisiblePayloadBinding(
        fragment_set.context_manifest_digest,
        fragment_set.fragment_set_digest,
        tuple(item.fragment_id for item in model_fragments),
        request_digest,
    )
    return payload, binding
