"""ZK-P1-004 production service -> recipe -> exact model payload E2E."""

from __future__ import annotations

import datetime as dt

from zekam.application.context_materializer import (
    materialize_recipe_fragments,
    serialize_model_visible_payload,
)
from zekam.application.context_ranking import (
    ContextCandidateSetIssuer,
    ContextRankingRequest,
    ContextRankingSnapshotIssuer,
    count_context_tokens,
)
from zekam.application.context_recipe import ContextRecipeRegistry, ContextRecipeRole
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)

NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _candidate(kind: ContextCandidateKind, content: str) -> ContextCandidate:
    return ContextCandidate(
        candidate_id=kind.value,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision="revision-1",
        content_digest=digest(content),
        token_count=count_context_tokens(content),
        kind=kind,
        source_ref=f"context/{kind.value}",
        scope_ref="work/e2e",
    )


def test_coordinator_recipe_sourceu_final_model_payloadindan_dislar() -> None:
    contents = {
        "system-policy": "Guvenli koordinasyon kurallari",
        "work-contract": "Exact isi subagentlara dagit",
        "run-status": "Bir builder calisiyor",
        "source-slice": "BU KOD COORDINATOR PAYLOADINA GIREMEZ",
    }
    candidates = tuple(
        _candidate(ContextCandidateKind(key), value) for key, value in contents.items()
    )
    snapshot = ContextRankingSnapshotIssuer.issue(
        request=ContextRankingRequest(
            role="coordinator",
            target_identity_refs=(),
            step_scope_ref="step/e2e",
            work_scope_ref="work/e2e",
            project_scope_ref="project/e2e",
            realm_scope_ref="realm/e2e",
            current_source_revision=None,
            compatible_source_revisions=(),
            task_terms=(),
            tokenizer_profile_digest=candidates[0].tokenizer_profile_digest,
        ),
        realm_ref="realm/e2e",
        project_ref="project/e2e",
        work_ref="work/e2e",
        step_ref="step/e2e",
        assignment_id="00000000-0000-0000-0000-000000000002",
        assignment_digest=digest("assignment/e2e"),
        source_snapshot_digest=digest("source/e2e"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(minutes=5),
    )
    candidate_set = ContextCandidateSetIssuer.issue(snapshot, candidates, contents, now=NOW)
    packet = ContextRecipeRegistry().compile(
        ContextRecipeRole.COORDINATOR,
        candidate_set,
        token_budget=5000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        ranking_snapshot=snapshot,
    )
    selected_contents = {
        item.candidate_id: contents[item.candidate_id] for item in packet.manifest.selected
    }
    fragment_set = materialize_recipe_fragments(packet, candidates, selected_contents)
    model_contents = {
        fragment.fragment_id: selected_contents[fragment.candidate_id]
        for fragment in fragment_set.fragments
    }
    payload, binding = serialize_model_visible_payload(
        fragment_set, model_contents, recipe_packet=packet
    )

    assert packet.recipe_excluded == ("source-slice",)
    assert all("BU KOD" not in message["content"] for message in payload["messages"])
    assert binding.context_manifest_digest == packet.manifest.manifest_digest
    assert binding.fragment_set_digest == fragment_set.fragment_set_digest
