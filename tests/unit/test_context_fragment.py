from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from zekam.application.context_materializer import (
    FragmentMaterialization,
    materialize_fragments,
    materialize_recipe_fragments,
    serialize_model_visible_payload,
)
from zekam.application.context_ranking import (
    ContextCandidateSetIssuer,
    ContextRankingRequest,
    ContextRankingSnapshotIssuer,
    count_context_tokens,
)
from zekam.application.context_recipe import (
    ContextRecipeRegistry,
    ContextRecipeRole,
    RecipeContextPacket,
)
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    compile_context,
)
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragment,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

NOW = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


def _candidate(candidate_id: str, content: str, *, required: bool = False) -> ContextCandidate:
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision=f"revision/{candidate_id}",
        content_digest=digest(content),
        token_count=5,
        required=required,
    )


def _materialized() -> tuple[ContextFragmentSet, dict[str, str]]:
    candidates = (
        _candidate("system", "Kurallari uygula", required=True),
        _candidate("work", "Siradaki adimi tamamla"),
    )
    manifest = compile_context(
        candidates,
        token_budget=20,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    fragment_set = materialize_fragments(
        manifest,
        candidates,
        (
            FragmentMaterialization(
                "system",
                ContextContentKind.SYSTEM_INSTRUCTION,
                ContextRole.SYSTEM,
                ContextVisibility.MODEL,
                "work/policy/current",
                "Kurallari uygula",
            ),
            FragmentMaterialization(
                "work",
                ContextContentKind.WORK_CONTEXT,
                ContextRole.USER,
                ContextVisibility.MODEL,
                "work/item/current",
                "Siradaki adimi tamamla",
            ),
        ),
    )
    return fragment_set, {
        "fragment/system": "Kurallari uygula",
        "fragment/work": "Siradaki adimi tamamla",
    }


def _recipe_materialized() -> tuple[RecipeContextPacket, ContextFragmentSet, dict[str, str]]:
    raw = {
        ContextCandidateKind.SYSTEM_POLICY: "Kurallari uygula",
        ContextCandidateKind.WORK_CONTRACT: "Siradaki adimi tamamla",
        ContextCandidateKind.RUN_STATUS: "Calisma aktif",
    }
    candidates = tuple(
        ContextCandidate(
            candidate_id=kind.value,
            authority=AuthorityLevel.VERIFIED,
            observed_at=NOW,
            source_revision=f"revision/{kind.value}",
            content_digest=digest(content),
            token_count=count_context_tokens(content),
            kind=kind,
            source_ref=f"context/{kind.value}",
            scope_ref="work/fragment",
        )
        for kind, content in raw.items()
    )
    snapshot = ContextRankingSnapshotIssuer.issue(
        request=ContextRankingRequest(
            role="coordinator",
            target_identity_refs=(),
            step_scope_ref="step/fragment",
            work_scope_ref="work/fragment",
            project_scope_ref="project/fragment",
            realm_scope_ref="realm/fragment",
            current_source_revision=None,
            compatible_source_revisions=(),
            task_terms=(),
            tokenizer_profile_digest=candidates[0].tokenizer_profile_digest,
        ),
        realm_ref="realm/fragment",
        project_ref="project/fragment",
        work_ref="work/fragment",
        step_ref="step/fragment",
        assignment_id="00000000-0000-0000-0000-000000000004",
        assignment_digest=digest("assignment/fragment"),
        source_snapshot_digest=digest("source/fragment"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(minutes=5),
    )
    raw_contents = {kind.value: content for kind, content in raw.items()}
    candidate_set = ContextCandidateSetIssuer.issue(snapshot, candidates, raw_contents, now=NOW)
    packet = ContextRecipeRegistry().compile(
        ContextRecipeRole.COORDINATOR,
        candidate_set,
        token_budget=100,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        ranking_snapshot=snapshot,
    )
    selected_contents = {item.candidate_id: raw[item.kind] for item in packet.manifest.selected}
    fragment_set = materialize_recipe_fragments(packet, candidates, selected_contents)
    return (
        packet,
        fragment_set,
        {
            f"fragment/{candidate_id}": content
            for candidate_id, content in selected_contents.items()
        },
    )


def test_selected_context_materializes_with_exact_kind_role_order_and_source() -> None:
    fragment_set, _ = _materialized()
    assert [item.order for item in fragment_set.fragments] == [0, 1]
    assert [item.content_kind.value for item in fragment_set.fragments] == [
        "system-instruction",
        "work-context",
    ]
    assert [item.role.value for item in fragment_set.fragments] == ["system", "user"]
    assert [item.source_ref for item in fragment_set.fragments] == [
        "work/policy/current",
        "work/item/current",
    ]
    assert fragment_set.body()["schema"] == "zekam-context-fragment-set/v2"


def test_materialization_rejects_missing_duplicate_and_content_drift() -> None:
    fragment_set, contents = _materialized()
    with pytest.raises(ValidationFailed, match="sirasi"):
        ContextFragmentSet(
            fragment_set.context_manifest_digest,
            (replace(fragment_set.fragments[0], order=1), fragment_set.fragments[1]),
        )
    with pytest.raises(PolicyViolation, match="recipe packet"):
        serialize_model_visible_payload(
            fragment_set,
            {**contents, "fragment/work": "degistirilmis"},
        )
    with pytest.raises(PolicyViolation, match="recipe packet"):
        serialize_model_visible_payload(fragment_set, {"fragment/system": "Kurallari uygula"})


def test_unknown_content_kind_is_rejected_before_it_can_become_model_visible() -> None:
    with pytest.raises(ValidationFailed, match="kind registry"):
        ContextFragment(
            fragment_id="fragment/unknown",
            candidate_id="unknown",
            content_kind="unknown",  # type: ignore[arg-type]
            role=ContextRole.USER,
            order=0,
            visibility=ContextVisibility.MODEL,
            authority=AuthorityLevel.OBSERVED,
            source_ref="source/unknown",
            source_revision="revision/1",
            content_digest=digest("unknown"),
            token_count=1,
            required=False,
        )


def test_serializer_binds_exact_ordered_model_payload_and_rejects_message_bypass() -> None:
    packet, fragment_set, contents = _recipe_materialized()
    payload, binding = serialize_model_visible_payload(
        fragment_set,
        contents,
        recipe_packet=packet,
        base_payload={"model": "provider/model", "temperature": 0},
    )
    assert payload["messages"] == [
        {"role": "user", "content": "Calisma aktif"},
        {"role": "system", "content": "Kurallari uygula"},
        {"role": "user", "content": "Siradaki adimi tamamla"},
    ]
    assert binding.request_payload_digest == digest(payload)
    assert binding.fragment_set_digest == fragment_set.fragment_set_digest
    assert binding.ordered_model_fragment_ids == (
        "fragment/run-status",
        "fragment/system-policy",
        "fragment/work-contract",
    )
    with pytest.raises(PolicyViolation, match="base payload"):
        serialize_model_visible_payload(
            fragment_set,
            contents,
            recipe_packet=packet,
            base_payload={"messages": []},
        )
