"""ZK-P1-004 role-based context recipe golden contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.context_materializer import materialize_recipe_fragments
from zekam.application.context_recipe import (
    DEFAULT_CONTEXT_RECIPES,
    ContextRecipe,
    ContextRecipeRegistry,
    ContextRecipeRole,
    RecipeContextPacket,
)
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _candidate(kind: ContextCandidateKind, *, suffix: str = "") -> ContextCandidate:
    candidate_id = f"{kind.value}{suffix}"
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision="revision-1",
        content_digest=digest(candidate_id),
        token_count=10,
        kind=kind,
        source_ref=f"context/{kind.value}",
    )


ALL_TYPED = (
    *(
        _candidate(kind)
        for kind in ContextCandidateKind
        if kind is not ContextCandidateKind.GENERAL
    ),
    _candidate(ContextCandidateKind.GENERAL),
)

GOLDEN_ALLOWED = {
    ContextRecipeRole.COORDINATOR: {
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.RUN_STATUS,
        ContextCandidateKind.CHECKPOINT,
        ContextCandidateKind.EFFECT_RECEIPT,
        ContextCandidateKind.VERIFICATION_RESULT,
        ContextCandidateKind.ARCHITECTURE_RULE,
        ContextCandidateKind.DEPENDENCY_MANIFEST,
        ContextCandidateKind.RESEARCH_EVIDENCE,
    },
    ContextRecipeRole.RESEARCHER: {
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.SOURCE_SLICE,
        ContextCandidateKind.RESEARCH_EVIDENCE,
        ContextCandidateKind.CITATION,
        ContextCandidateKind.KNOWLEDGE,
        ContextCandidateKind.MEMORY_SUMMARY,
        ContextCandidateKind.ARCHITECTURE_RULE,
        ContextCandidateKind.DEPENDENCY_MANIFEST,
        ContextCandidateKind.CHECKPOINT,
    },
    ContextRecipeRole.BUILDER: {
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.ARCHITECTURE_RULE,
        ContextCandidateKind.DEPENDENCY_MANIFEST,
        ContextCandidateKind.SOURCE_SLICE,
        ContextCandidateKind.SOURCE_DIFF,
        ContextCandidateKind.KNOWLEDGE,
        ContextCandidateKind.RESEARCH_EVIDENCE,
        ContextCandidateKind.EFFECT_RECEIPT,
        ContextCandidateKind.TOOL_RESULT_SUMMARY,
        ContextCandidateKind.TEST_EVIDENCE,
        ContextCandidateKind.CHECKPOINT,
    },
    ContextRecipeRole.VERIFIER: {
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.ARCHITECTURE_RULE,
        ContextCandidateKind.DEPENDENCY_MANIFEST,
        ContextCandidateKind.SOURCE_SLICE,
        ContextCandidateKind.SOURCE_DIFF,
        ContextCandidateKind.EFFECT_RECEIPT,
        ContextCandidateKind.VERIFICATION_RESULT,
        ContextCandidateKind.TEST_EVIDENCE,
        ContextCandidateKind.CHECKPOINT,
    },
}

GOLDEN_ORDER = {
    ContextRecipeRole.COORDINATOR: (
        "run-status",
        "system-policy",
        "work-contract",
        "architecture-rule",
        "checkpoint",
        "dependency-manifest",
        "effect-receipt",
        "research-evidence",
        "verification-result",
    ),
    ContextRecipeRole.RESEARCHER: (
        "system-policy",
        "work-contract",
        "architecture-rule",
        "checkpoint",
        "citation",
        "dependency-manifest",
        "knowledge",
        "memory-summary",
        "research-evidence",
        "source-slice",
    ),
    ContextRecipeRole.BUILDER: (
        "architecture-rule",
        "dependency-manifest",
        "source-slice",
        "system-policy",
        "work-contract",
        "checkpoint",
        "effect-receipt",
        "knowledge",
        "research-evidence",
        "source-diff",
        "test-evidence",
        "tool-result-summary",
    ),
    ContextRecipeRole.VERIFIER: (
        "effect-receipt",
        "source-diff",
        "system-policy",
        "test-evidence",
        "work-contract",
        "architecture-rule",
        "checkpoint",
        "dependency-manifest",
        "source-slice",
        "verification-result",
    ),
}


@pytest.mark.parametrize("role", tuple(ContextRecipeRole))
def test_role_recipe_golden_packet(role: ContextRecipeRole) -> None:
    registry = ContextRecipeRegistry()
    packet = registry.compile(
        role,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    selected = {
        next(item.kind for item in ALL_TYPED if item.candidate_id == selection.candidate_id)
        for selection in packet.manifest.selected
    }
    assert selected == GOLDEN_ALLOWED[role]
    assert tuple(item.candidate_id for item in packet.manifest.selected) == GOLDEN_ORDER[role]
    assert packet.manifest.token_budget == registry.for_role(role).maximum_token_budget
    assert packet.recipe_digest == registry.for_role(role).recipe_digest
    assert packet.body()["grants_authority"] is False
    assert packet.packet_digest.startswith("sha256:")


def test_coordinator_source_ve_codebase_context_alamaz() -> None:
    packet = ContextRecipeRegistry().compile(
        ContextRecipeRole.COORDINATOR,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    assert ContextCandidateKind.SOURCE_SLICE not in GOLDEN_ALLOWED[ContextRecipeRole.COORDINATOR]
    assert "source-slice" in packet.recipe_excluded
    sizes = {
        role: len(
            ContextRecipeRegistry()
            .compile(
                role,
                ALL_TYPED,
                token_budget=20_000,
                minimum_authority=AuthorityLevel.OBSERVED,
                now=NOW,
            )
            .manifest.selected
        )
        for role in ContextRecipeRole
    }
    assert sizes[ContextRecipeRole.COORDINATOR] < min(
        size for role, size in sizes.items() if role is not ContextRecipeRole.COORDINATOR
    )
    with pytest.raises(PolicyViolation, match="Coordinator"):
        ContextRecipe(
            "unsafe-coordinator",
            1,
            ContextRecipeRole.COORDINATOR,
            frozenset({ContextCandidateKind.SOURCE_SLICE}),
            frozenset({ContextCandidateKind.SOURCE_SLICE}),
            100,
            1,
            100,
        )


def test_recipe_materialization_kind_role_ve_source_refi_registryden_turetir() -> None:
    packet = ContextRecipeRegistry().compile(
        ContextRecipeRole.BUILDER,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    selected = {item.candidate_id for item in packet.manifest.selected}
    fragment_set = materialize_recipe_fragments(
        packet,
        ALL_TYPED,
        {candidate_id: candidate_id for candidate_id in selected},
    )
    system = next(item for item in fragment_set.fragments if item.candidate_id == "system-policy")
    source = next(item for item in fragment_set.fragments if item.candidate_id == "source-slice")
    assert system.role.value == "system"
    assert system.content_kind.value == "system-instruction"
    assert source.role.value == "user"
    assert source.content_kind.value == "knowledge"
    assert source.source_ref == "context/source-slice"
    drifted = tuple(
        replace(item, source_ref="context/forged")
        if item.kind is ContextCandidateKind.SOURCE_SLICE
        else item
        for item in ALL_TYPED
    )
    with pytest.raises(PolicyViolation, match="binding drift"):
        materialize_recipe_fragments(
            packet,
            drifted,
            {candidate_id: candidate_id for candidate_id in selected},
        )
    content_drift = tuple(
        replace(item, content_digest=digest("forged"), token_count=item.token_count + 1)
        if item.kind is ContextCandidateKind.SOURCE_SLICE
        else item
        for item in ALL_TYPED
    )
    with pytest.raises(PolicyViolation, match="binding drift"):
        materialize_recipe_fragments(
            packet,
            content_drift,
            {
                candidate_id: "forged" if candidate_id == "source-slice" else candidate_id
                for candidate_id in selected
            },
        )


def test_production_context_service_role_recipe_zorlar() -> None:
    packet = ContextContinuityService().compile(
        ALL_TYPED,
        role=ContextRecipeRole.COORDINATOR,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    assert packet.role is ContextRecipeRole.COORDINATOR
    assert "source-slice" in packet.recipe_excluded


def test_recipe_required_kind_eksiginde_fail_closed() -> None:
    candidates = tuple(
        item for item in ALL_TYPED if item.kind is not ContextCandidateKind.SOURCE_SLICE
    )
    with pytest.raises(PolicyViolation, match="source-slice"):
        ContextRecipeRegistry().compile(
            ContextRecipeRole.BUILDER,
            candidates,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )


def test_recipe_required_context_budget_disinda_birakilamaz() -> None:
    with pytest.raises(PolicyViolation, match="Required context token budget"):
        ContextRecipeRegistry().compile(
            ContextRecipeRole.VERIFIER,
            ALL_TYPED,
            token_budget=20,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )


def test_recipe_secimi_candidate_sirasindan_bagimsizdir() -> None:
    registry = ContextRecipeRegistry()
    first = registry.compile(
        ContextRecipeRole.RESEARCHER,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    second = registry.compile(
        ContextRecipeRole.RESEARCHER,
        tuple(reversed(ALL_TYPED)),
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    assert first.packet_digest == second.packet_digest


def test_recipe_stale_ve_cross_role_replay_reddedilir() -> None:
    registry = ContextRecipeRegistry()
    packet = registry.compile(
        ContextRecipeRole.RESEARCHER,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    registry.validate_packet(packet, ContextRecipeRole.RESEARCHER)
    with pytest.raises(PolicyViolation, match="cross-role"):
        registry.validate_packet(packet, ContextRecipeRole.BUILDER)
    changed = replace(
        registry.for_role(ContextRecipeRole.RESEARCHER), version=2, recipe_id="researcher-v2"
    )
    stale_registry = ContextRecipeRegistry(
        tuple(
            changed if item.role is ContextRecipeRole.RESEARCHER else item
            for item in registry.recipes
        )
    )
    with pytest.raises(PolicyViolation, match="stale"):
        stale_registry.validate_packet(packet, ContextRecipeRole.RESEARCHER)


def test_coordinator_packet_semantic_forgery_reddedilir() -> None:
    registry = ContextRecipeRegistry()
    coordinator = registry.compile(
        ContextRecipeRole.COORDINATOR,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    builder = registry.compile(
        ContextRecipeRole.BUILDER,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    source = next(
        item for item in builder.manifest.selected if item.kind is ContextCandidateKind.SOURCE_SLICE
    )
    forged_manifest = replace(
        coordinator.manifest,
        selected=(*coordinator.manifest.selected, source),
        candidate_fingerprint=digest("forged-candidate-partition"),
    )
    forged = RecipeContextPacket(
        coordinator.recipe_id,
        coordinator.recipe_digest,
        coordinator.role,
        coordinator.requested_token_budget,
        forged_manifest,
        coordinator.recipe_excluded,
        coordinator.issuance_seal,
    )
    with pytest.raises(PolicyViolation, match="issuance provenance"):
        registry.validate_packet(forged, ContextRecipeRole.COORDINATOR)


def test_allowed_kind_selection_provenance_forgery_reddedilir() -> None:
    registry = ContextRecipeRegistry()
    packet = registry.compile(
        ContextRecipeRole.COORDINATOR,
        ALL_TYPED,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    forged_source = _candidate(ContextCandidateKind.RESEARCH_EVIDENCE, suffix="-forged")
    forged_selection = next(
        item
        for item in registry.compile(
            ContextRecipeRole.COORDINATOR,
            (*ALL_TYPED, forged_source),
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        ).manifest.selected
        if item.candidate_id == forged_source.candidate_id
    )
    forged_manifest = replace(
        packet.manifest,
        selected=(*packet.manifest.selected, forged_selection),
        candidate_fingerprint=digest("made-up-provenance"),
    )
    forged = RecipeContextPacket(
        packet.recipe_id,
        packet.recipe_digest,
        packet.role,
        packet.requested_token_budget,
        forged_manifest,
        packet.recipe_excluded,
        packet.issuance_seal,
    )
    with pytest.raises(PolicyViolation, match="issuance provenance"):
        registry.validate_packet(forged, ContextRecipeRole.COORDINATOR)


def test_excluded_required_ve_duplicate_required_kind_reddedilir() -> None:
    registry = ContextRecipeRegistry()
    forbidden = tuple(
        replace(item, required=True) if item.kind is ContextCandidateKind.SOURCE_SLICE else item
        for item in ALL_TYPED
    )
    with pytest.raises(PolicyViolation, match="Excluded"):
        registry.compile(
            ContextRecipeRole.COORDINATOR,
            forbidden,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )
    duplicate = (*ALL_TYPED, _candidate(ContextCandidateKind.SYSTEM_POLICY, suffix="-other"))
    with pytest.raises(PolicyViolation, match="tekil"):
        registry.compile(
            ContextRecipeRole.RESEARCHER,
            duplicate,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )


def test_recipe_per_kind_cardinality_ve_token_limitini_zorlar() -> None:
    registry = ContextRecipeRegistry()
    too_many = ALL_TYPED + tuple(
        _candidate(ContextCandidateKind.RUN_STATUS, suffix=f"-{index}") for index in range(2)
    )
    with pytest.raises(PolicyViolation, match="candidate limiti"):
        registry.compile(
            ContextRecipeRole.COORDINATOR,
            too_many,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )
    huge = tuple(
        replace(item, token_count=601) if item.kind is ContextCandidateKind.RUN_STATUS else item
        for item in ALL_TYPED
    )
    with pytest.raises(PolicyViolation, match="token limiti"):
        registry.compile(
            ContextRecipeRole.COORDINATOR,
            huge,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )


def test_recipe_registry_duplicate_role_ve_untyped_kind_reddeder() -> None:
    with pytest.raises(ValidationFailed, match="tek current"):
        ContextRecipeRegistry(
            (
                DEFAULT_CONTEXT_RECIPES[0],
                replace(DEFAULT_CONTEXT_RECIPES[0], recipe_id="coordinator-v2", version=2),
            )
        )
    with pytest.raises(PolicyViolation, match="untyped general"):
        ContextRecipe(
            "general-builder",
            1,
            ContextRecipeRole.BUILDER,
            frozenset({ContextCandidateKind.GENERAL}),
            frozenset({ContextCandidateKind.GENERAL}),
            100,
            1,
            100,
        )
