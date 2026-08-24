"""P1-005 dort rol golden context safety/quality/cost degerlendirmesi."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_quality import ContextGoldenCase, evaluate_context_quality
from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    OmittedReason,
)

NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _candidate(
    candidate_id: str,
    content: str,
    role: str,
    *,
    required: bool = False,
    revision: str = "revision/current",
    candidate_role: str | None = None,
    conflict: bool = False,
) -> ContextCandidate:
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision=revision,
        content_digest=digest(content),
        token_count=count_context_tokens(content),
        required=required,
        kind=ContextCandidateKind.CITATION,
        source_ref=f"context/{candidate_id}",
        identity_refs=("work/golden",),
        scope_ref="work/golden",
        applicable_roles=(candidate_role or role,),
        task_terms=("oracle",),
        conflict_refs=(("conflict/direct",) if conflict else ()),
    )


def _compile(role: str, candidates: tuple[ContextCandidate, ...], contents: dict[str, str]):
    return compile_context_v2(
        candidates,
        ranking_request=ContextRankingRequest(
            role=role,
            target_identity_refs=("work/golden",),
            step_scope_ref="step/golden",
            work_scope_ref="work/golden",
            project_scope_ref="project/golden",
            realm_scope_ref="realm/golden",
            current_source_revision="revision/current",
            compatible_source_revisions=("revision/parent",),
            task_terms=("oracle",),
            tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
        ),
        token_budget=500,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        recipe_id=f"{role}-golden-v1",
        recipe_digest=digest(f"recipe/{role}"),
        target_role=role,
        contents=contents,
        ranking_snapshot_digest=digest(f"snapshot/{role}"),
        candidate_set_digest=digest(f"candidate-set/{role}"),
    )


@pytest.mark.parametrize("role", ("coordinator", "researcher", "builder", "verifier"))
def test_dort_rol_golden_safety_quality_ve_cost_kapisi(role: str) -> None:
    candidates = (
        _candidate("required", "required", role, required=True),
        _candidate("compatible", "shared", role, revision="revision/parent"),
        _candidate("duplicate", "shared", role, revision="revision/parent"),
        _candidate("citation", "citation", role),
        _candidate("conflict", "conflict", role, conflict=True),
        _candidate("forbidden", "forbidden", role, candidate_role="other-role"),
    )
    contents = {
        "required": "required",
        "compatible": "shared",
        "duplicate": "shared",
        "citation": "citation",
        "conflict": "conflict",
        "forbidden": "forbidden",
    }
    manifest = _compile(role, candidates, contents)
    reordered = _compile(role, tuple(reversed(candidates)), contents)
    case = ContextGoldenCase(
        case_id=f"{role}-oracle-golden-001",
        role=role,
        required_candidate_ids=frozenset({"required"}),
        forbidden_candidate_ids=frozenset({"forbidden"}),
        relevant_candidate_ids=frozenset({"required", "compatible", "citation"}),
        citation_candidate_ids=frozenset({"citation"}),
        conflict_candidate_ids=frozenset({"conflict"}),
        compatible_revision_candidate_ids=frozenset({"compatible"}),
        expected_omission_reasons=(
            ("conflict", OmittedReason.CONFLICT),
            ("duplicate", OmittedReason.DUPLICATE),
            ("forbidden", OmittedReason.ROLE_MISMATCH),
        ),
        max_tokens=500,
    )
    selected_tokens = sum(item.token_count for item in manifest.selected)
    result = evaluate_context_quality(
        case,
        manifest,
        reordered_manifest=reordered,
        verified_outcome_quality_ppm=900_000,
        baseline_outcome_quality_ppm=900_000,
        baseline_selected_tokens=selected_tokens + count_context_tokens("shared"),
    )
    assert result.required_recall_ppm == 1_000_000
    assert result.forbidden_inclusion_count == 0
    assert result.relevance_recall_ppm == 1_000_000
    assert result.citation_coverage_ppm == 1_000_000
    assert result.conflict_visibility_ppm == 1_000_000
    assert result.source_revision_compatibility_ppm == 1_000_000
    assert result.ordering_stability_ppm == 1_000_000
    assert result.role_leakage_count == 0
    assert result.quality_or_cost_improved
    assert result.metrics_digest.startswith("sha256:")
