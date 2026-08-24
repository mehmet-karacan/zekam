"""ZK-P1-005 compiler v2 ranking, dedupe, partition ve metrics testleri."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    OmittedReason,
)
from zekam.domain.context_scoring import CONTEXT_SCORING_POLICY_DIGEST
from zekam.domain.errors import PolicyViolation

NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _request() -> ContextRankingRequest:
    return ContextRankingRequest(
        role="builder",
        target_identity_refs=("entity/task",),
        step_scope_ref="step/current",
        work_scope_ref="work/current",
        project_scope_ref="project/current",
        realm_scope_ref="realm/current",
        current_source_revision="revision/current",
        compatible_source_revisions=("revision/parent",),
        task_terms=("java", "oracle"),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )


def _candidate(
    candidate_id: str,
    *,
    content: str | None = None,
    tokens: int = 10,
    required: bool = False,
    revision: str = "revision/current",
    scope: str = "work/current",
    role: str = "builder",
) -> ContextCandidate:
    value = content if content is not None else candidate_id.ljust(tokens, "x")[:tokens]
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision=revision,
        content_digest=digest(value),
        token_count=len(value.encode("utf-8")),
        required=required,
        kind=ContextCandidateKind.SOURCE_SLICE,
        source_ref=f"context/{candidate_id}",
        identity_refs=("entity/task",),
        scope_ref=scope,
        applicable_roles=(role,),
        task_terms=("java",),
    )


def _compile(candidates: tuple[ContextCandidate, ...], budget: int = 30):
    possible = (
        "same",
        *(
            item.candidate_id.ljust(item.token_count, "x")[: item.token_count]
            for item in candidates
        ),
    )
    contents = {
        item.candidate_id: next(value for value in possible if digest(value) == item.content_digest)
        for item in candidates
    }
    return compile_context_v2(
        candidates,
        ranking_request=_request(),
        token_budget=budget,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        recipe_id="builder-v1",
        recipe_digest=digest("recipe"),
        target_role="builder",
        contents=contents,
        ranking_snapshot_digest=digest("ranking-snapshot"),
        candidate_set_digest=digest("candidate-set"),
    )


def test_exact_duplicate_budget_oncesi_bastirilir_ve_token_tasarrufu_olculur() -> None:
    manifest = _compile(
        (
            _candidate("canonical", content="same"),
            _candidate("duplicate", content="same"),
            _candidate("unique", tokens=20),
        )
    )
    assert {item.candidate_id for item in manifest.selected} == {"canonical", "unique"}
    omission = next(item for item in manifest.omitted if item.candidate_id == "duplicate")
    assert omission.reason is OmittedReason.DUPLICATE
    assert omission.canonical_candidate_id == "canonical"
    assert omission.group_digest is not None
    metrics = manifest.compiler_metrics
    assert metrics is not None
    assert metrics.duplicate_suppressed_count == 1
    assert metrics.duplicate_suppressed_tokens == 4
    assert metrics.duplicate_token_ratio_ppm == 142_857
    assert metrics.token_utilization_ppm == 800_000
    assert manifest.scoring_policy_digest == CONTEXT_SCORING_POLICY_DIGEST


def test_farkli_revision_ve_scope_duplicate_sayilmaz_acik_nedenle_omitted_olur() -> None:
    manifest = _compile(
        (
            _candidate("current", content="same"),
            _candidate("old", content="same", revision="revision/old"),
            _candidate("external", content="same", scope="project/other"),
            _candidate("other-role", role="researcher"),
        )
    )
    reasons = {item.candidate_id: item.reason for item in manifest.omitted}
    assert reasons == {
        "external": OmittedReason.SCOPE_MISMATCH,
        "old": OmittedReason.SOURCE_REVISION_MISMATCH,
        "other-role": OmittedReason.ROLE_MISMATCH,
    }


def test_compiler_v2_candidate_sirasindan_bagimsiz_exact_partition_uretir() -> None:
    candidates = (
        _candidate("a", tokens=9),
        _candidate("b", tokens=10),
        _candidate("c", tokens=11),
    )
    digests = {
        _compile(tuple(ordering), budget=20).manifest_digest
        for ordering in itertools.permutations(candidates)
    }
    assert len(digests) == 1
    manifest = _compile(candidates, budget=20)
    assert {item.candidate_id for item in manifest.selected} | {
        item.candidate_id for item in manifest.omitted
    } == {"a", "b", "c"}
    assert not (
        {item.candidate_id for item in manifest.selected}
        & {item.candidate_id for item in manifest.omitted}
    )


def test_required_duplicate_ve_required_feature_rejection_fail_closed() -> None:
    with pytest.raises(PolicyViolation, match="Required duplicate"):
        _compile(
            (
                _candidate("first", content="same", required=True),
                _candidate("second", content="same", required=True),
            )
        )
    with pytest.raises(PolicyViolation, match="source-revision-mismatch"):
        _compile((_candidate("required-old", required=True, revision="revision/old"),))


def test_score_v2_exact_identity_scope_ve_token_maliyetini_aciklar() -> None:
    manifest = _compile(
        (
            _candidate("large", tokens=20),
            _candidate("small", tokens=5),
        ),
        budget=5,
    )
    assert tuple(item.candidate_id for item in manifest.selected) == ("small",)
    selected = manifest.selected[0]
    assert selected.reason == "context-score-v2"
    assert selected.score[-2] == -5
    assert selected.score[-1] == "small"
    assert "token-efficiency" in selected.reason_codes
