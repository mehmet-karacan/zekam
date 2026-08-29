"""WP03 bounded LoopProgressPacket context integration tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.context_materializer import materialize_recipe_fragments
from zekam.application.context_ranking import (
    ContextCandidateSetIssuer,
    ContextRankingRequest,
    ContextRankingSnapshotIssuer,
    count_context_tokens,
)
from zekam.application.context_recipe import ContextRecipeRegistry, ContextRecipeRole
from zekam.application.loop_progress_hydration import (
    CurrentLoopContextBinding,
    build_loop_progress_hydration,
)
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.loop_progress import LoopProgressPacket
from zekam.domain.optimization import ProgressState, ProgressVector

NOW = dt.datetime(2026, 8, 29, tzinfo=dt.UTC)


def _vector(reason: str) -> ProgressVector:
    return ProgressVector((), (), (), (), (), (), None, ProgressState.INVALID, (reason,))


def _packet() -> LoopProgressPacket:
    previous = _vector("baseline-not-measured")
    current = _vector("measurement-pending")
    return LoopProgressPacket(
        digest("objective"),
        "source-7",
        digest("plan"),
        digest("policy"),
        digest("validator-assets"),
        digest("artifact-before"),
        digest("artifact-after"),
        uuid4(),
        2,
        previous,
        current,
        current.deltas,
        digest("accepted-hypothesis"),
        (digest("rejected-hypothesis"),),
        digest("patch"),
        digest("failure"),
        "verification/diagnosis",
        digest("diagnosis"),
        (("evidence/metric", digest("metric-evidence")),),
        2,
        500,
        1_000,
        60,
        "inspect failing boundary",
        ("retry/same-patch",),
        1024,
    )


def _binding() -> CurrentLoopContextBinding:
    packet = _packet()
    return CurrentLoopContextBinding(
        packet.objective_digest,
        packet.source_revision,
        packet.plan_digest,
        packet.policy_revision_digest,
        packet.validator_asset_manifest_digest,
    )


def _candidate(kind: ContextCandidateKind) -> tuple[ContextCandidate, str]:
    content = kind.value
    return (
        ContextCandidate(
            candidate_id=kind.value,
            authority=AuthorityLevel.CANONICAL,
            observed_at=NOW,
            source_revision="source-7",
            content_digest=digest(content),
            token_count=count_context_tokens(content),
            required=True,
            kind=kind,
            source_ref=f"context/{kind.value}",
            identity_refs=("work/active",),
            scope_ref="scope/work",
            applicable_roles=("builder",),
        ),
        content,
    )


def _snapshot():  # type: ignore[no-untyped-def]
    scope = "scope/work"
    request = ContextRankingRequest(
        role="builder",
        target_identity_refs=("work/active",),
        step_scope_ref=scope,
        work_scope_ref=scope,
        project_scope_ref="scope/project",
        realm_scope_ref="scope/realm",
        current_source_revision="source-7",
        compatible_source_revisions=(),
        task_terms=(),
        tokenizer_profile_digest=_candidate(ContextCandidateKind.SYSTEM_POLICY)[
            0
        ].tokenizer_profile_digest,
    )
    return ContextRankingSnapshotIssuer.issue(
        request=request,
        realm_ref="scope/realm",
        project_ref="scope/project",
        work_ref=scope,
        step_ref=scope,
        assignment_id="00000000-0000-0000-0000-000000000001",
        assignment_digest=digest("assignment/builder"),
        source_snapshot_digest=digest("source/snapshot"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(minutes=5),
    )


def test_attempt2_packet_is_required_bounded_and_materialized_as_checkpoint() -> None:
    packet = _packet()
    hydration = build_loop_progress_hydration(
        packet,
        current=_binding(),
        observed_at=NOW,
        identity_refs=("work/active",),
        scope_ref="scope/work",
        role="builder",
        authority=AuthorityLevel.CANONICAL,
    )
    assert hydration.packet_digest == packet.packet_digest
    assert hydration.candidate.required
    assert hydration.candidate.kind is ContextCandidateKind.LOOP_PROGRESS_PACKET
    assert "raw_transcript" not in hydration.content
    assert "messages" not in hydration.content

    base = tuple(
        _candidate(kind)
        for kind in (
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.SOURCE_SLICE,
        )
    )
    candidates = (*tuple(item for item, _content in base), hydration.candidate)
    contents = {
        **{item.candidate_id: content for item, content in base},
        hydration.candidate.candidate_id: hydration.content,
    }
    snapshot = _snapshot()
    candidate_set = ContextCandidateSetIssuer.issue(snapshot, candidates, contents, now=NOW)
    recipe = ContextRecipeRegistry().compile(
        ContextRecipeRole.BUILDER,
        candidate_set,
        token_budget=12_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        ranking_snapshot=snapshot,
        loop_attempt_ordinal=2,
        loop_progress_packet_digest=packet.packet_digest,
    )
    progress = tuple(
        item
        for item in recipe.manifest.selected
        if item.kind is ContextCandidateKind.LOOP_PROGRESS_PACKET
    )
    assert len(progress) == 1
    assert "required" in progress[0].reason_codes
    selected_contents = {
        item.candidate_id: contents[item.candidate_id] for item in recipe.manifest.selected
    }
    fragments = materialize_recipe_fragments(
        recipe,
        candidates,
        selected_contents,
    )
    fragment = next(
        item
        for item in fragments.fragments
        if item.candidate_id == hydration.candidate.candidate_id
    )
    assert fragment.content_kind.value == "checkpoint"


def test_attempt2_missing_stale_or_over_budget_packet_fails_closed() -> None:
    packet = _packet()
    with pytest.raises(PolicyViolation, match="stale"):
        build_loop_progress_hydration(
            packet,
            current=replace(_binding(), plan_digest=digest("stale-plan")),
            observed_at=NOW,
            identity_refs=("work/active",),
            scope_ref="scope/work",
            role="builder",
        )
    with pytest.raises(PolicyViolation, match="token budget"):
        replace(packet, max_packet_tokens=1)

    base = tuple(
        _candidate(kind)
        for kind in (
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.SOURCE_SLICE,
        )
    )
    snapshot = _snapshot()
    candidates = tuple(item for item, _content in base)
    contents = {item.candidate_id: content for item, content in base}
    candidate_set = ContextCandidateSetIssuer.issue(snapshot, candidates, contents, now=NOW)
    with pytest.raises(PolicyViolation, match="progress packet"):
        ContextRecipeRegistry().compile(
            ContextRecipeRole.BUILDER,
            candidate_set,
            token_budget=12_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
            ranking_snapshot=snapshot,
            loop_attempt_ordinal=2,
            loop_progress_packet_digest=packet.packet_digest,
        )
