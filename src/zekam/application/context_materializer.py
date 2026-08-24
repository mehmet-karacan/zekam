"""Materialize selected context into typed fragments and the exact provider payload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zekam.application.context_recipe import ContextRecipeRegistry, RecipeContextPacket
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import ContextCandidate, ContextCandidateKind, ContextManifest
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


_RECIPE_FRAGMENT_KIND = {
    ContextCandidateKind.SYSTEM_POLICY: ContextContentKind.SYSTEM_INSTRUCTION,
    ContextCandidateKind.WORK_CONTRACT: ContextContentKind.WORK_CONTEXT,
    ContextCandidateKind.RUN_STATUS: ContextContentKind.WORK_CONTEXT,
    ContextCandidateKind.ARCHITECTURE_RULE: ContextContentKind.KNOWLEDGE,
    ContextCandidateKind.DEPENDENCY_MANIFEST: ContextContentKind.KNOWLEDGE,
    ContextCandidateKind.SOURCE_SLICE: ContextContentKind.KNOWLEDGE,
    ContextCandidateKind.SOURCE_DIFF: ContextContentKind.WORK_CONTEXT,
    ContextCandidateKind.RESEARCH_EVIDENCE: ContextContentKind.KNOWLEDGE,
    ContextCandidateKind.CITATION: ContextContentKind.KNOWLEDGE,
    ContextCandidateKind.KNOWLEDGE: ContextContentKind.KNOWLEDGE,
    ContextCandidateKind.MEMORY_SUMMARY: ContextContentKind.MEMORY,
    ContextCandidateKind.EFFECT_RECEIPT: ContextContentKind.TOOL_RESULT,
    ContextCandidateKind.VERIFICATION_RESULT: ContextContentKind.TOOL_RESULT,
    ContextCandidateKind.TOOL_RESULT_SUMMARY: ContextContentKind.TOOL_RESULT,
    ContextCandidateKind.TEST_EVIDENCE: ContextContentKind.TOOL_RESULT,
    ContextCandidateKind.CHECKPOINT: ContextContentKind.CHECKPOINT,
}


def materialize_recipe_fragments(
    packet: RecipeContextPacket,
    candidates: tuple[ContextCandidate, ...],
    contents: Mapping[str, str],
    *,
    registry: ContextRecipeRegistry | None = None,
) -> ContextFragmentSet:
    """Recipe kind/role/visibility/source mappingini caller seciminden korur."""

    effective_registry = registry or ContextRecipeRegistry()
    effective_registry.validate_packet(packet, packet.role)
    selected_ids = tuple(item.candidate_id for item in packet.manifest.selected)
    candidate_by_id = {item.candidate_id: item for item in candidates}
    if len(candidate_by_id) != len(candidates) or set(contents) != set(selected_ids):
        raise PolicyViolation("Recipe materialization exact selected content partition ister")
    fragments: list[ContextFragment] = []
    for order, selected in enumerate(packet.manifest.selected):
        candidate = candidate_by_id.get(selected.candidate_id)
        if candidate is None or candidate.kind not in _RECIPE_FRAGMENT_KIND:
            raise PolicyViolation("Recipe selected candidate veya typed kind bulunamadi")
        if (
            candidate.kind is not selected.kind
            or candidate.source_ref != selected.source_ref
            or candidate.source_revision != selected.source_revision
            or candidate.content_digest != selected.content_digest
            or candidate.token_count != selected.token_count
            or candidate.score(packet.manifest.created_at) != selected.score
            or candidate.candidate_digest != selected.candidate_digest
        ):
            raise PolicyViolation("Recipe selected candidate kind/source binding drift")
        content = contents[selected.candidate_id]
        if digest(content) != selected.content_digest:
            raise PolicyViolation("Recipe materialized content digest mismatch")
        fragments.append(
            ContextFragment(
                fragment_id=f"fragment/{selected.candidate_id}",
                candidate_id=selected.candidate_id,
                content_kind=_RECIPE_FRAGMENT_KIND[candidate.kind],
                role=(
                    ContextRole.SYSTEM
                    if candidate.kind is ContextCandidateKind.SYSTEM_POLICY
                    else ContextRole.USER
                ),
                order=order,
                visibility=ContextVisibility.MODEL,
                authority=candidate.authority,
                source_ref=candidate.source_ref,
                source_revision=candidate.source_revision,
                content_digest=candidate.content_digest,
                token_count=candidate.token_count,
                required=selected.reason == "required-first",
            )
        )
    return ContextFragmentSet(packet.manifest.manifest_digest, tuple(fragments))


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
    recipe_packet: RecipeContextPacket | None = None,
    base_payload: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ModelVisiblePayloadBinding]:
    if recipe_packet is None:
        raise PolicyViolation("Provider model payload role recipe packet ister")
    ContextRecipeRegistry().validate_packet(recipe_packet, recipe_packet.role)
    if fragment_set.context_manifest_digest != recipe_packet.manifest.manifest_digest:
        raise PolicyViolation("Provider fragment set recipe manifest binding drift")
    expected = recipe_packet.manifest.selected
    if tuple(item.candidate_id for item in fragment_set.fragments) != tuple(
        item.candidate_id for item in expected
    ):
        raise PolicyViolation("Provider fragment set recipe selection/order drift")
    for fragment, selected in zip(fragment_set.fragments, expected, strict=True):
        expected_role = (
            ContextRole.SYSTEM
            if selected.kind is ContextCandidateKind.SYSTEM_POLICY
            else ContextRole.USER
        )
        if (
            fragment.content_kind is not _RECIPE_FRAGMENT_KIND[selected.kind]
            or fragment.role is not expected_role
            or fragment.visibility is not ContextVisibility.MODEL
            or fragment.source_ref != selected.source_ref
            or fragment.source_revision != selected.source_revision
            or fragment.content_digest != selected.content_digest
            or fragment.token_count != selected.token_count
            or int(fragment.authority) != selected.score[0]
            or fragment.required != (selected.reason == "required-first")
        ):
            raise PolicyViolation("Provider fragment recipe projection drift")
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
