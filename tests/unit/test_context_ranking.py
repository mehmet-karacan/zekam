"""Context feature builder current-state ve duplicate guvenlik testleri."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from zekam.application.context_ranking import (
    ContextRankingFeatureBuilder,
    ContextRankingRequest,
    ContextRankingSnapshotIssuer,
    count_context_tokens,
)
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    EvidenceReference,
)
from zekam.domain.context_scoring import ScopeProximity, SourceRevisionState
from zekam.domain.errors import PolicyViolation

NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _request() -> ContextRankingRequest:
    return ContextRankingRequest(
        role="builder",
        target_identity_refs=("entity/SKYRSM-1",),
        step_scope_ref="step/one",
        work_scope_ref="work/one",
        project_scope_ref="project/one",
        realm_scope_ref="realm/one",
        current_source_revision="source/current",
        compatible_source_revisions=("source/parent",),
        task_terms=("oracle", "migration"),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )


def _candidate(name: str, *, content: str = "same") -> ContextCandidate:
    return ContextCandidate(
        candidate_id=name,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision="source/current",
        content_digest=digest(content),
        token_count=count_context_tokens(content),
        evidence_refs=(EvidenceReference("source", f"source/{name}", digest(name)),),
        kind=ContextCandidateKind.SOURCE_SLICE,
        source_ref=f"context/{name}",
        identity_refs=("entity/SKYRSM-1",),
        scope_ref="work/one",
        applicable_roles=("builder",),
        task_terms=("oracle",),
    )


def test_feature_builder_exact_scope_revision_evidence_ve_relevance_turetir() -> None:
    features = ContextRankingFeatureBuilder(_request()).build_all(
        (_candidate("candidate", content="unique"),), {"candidate": "unique"}, now=NOW
    )["candidate"]
    assert features.exact_identity
    assert features.scope_proximity is ScopeProximity.WORK
    assert features.source_revision_state is SourceRevisionState.CURRENT
    assert features.evidence_strength == 1
    assert features.role_relevance == 4
    assert features.task_relevance == 2
    assert features.duplicate_group_digest is None


def test_exact_duplicate_ayni_kind_scope_revision_ve_icerikle_sinirli() -> None:
    first = _candidate("first")
    second = _candidate("second")
    different_revision = replace(second, candidate_id="revision", source_revision="source/parent")
    different_scope = replace(second, candidate_id="scope", scope_ref="project/one")
    features = ContextRankingFeatureBuilder(_request()).build_all(
        (first, second, different_revision, different_scope),
        {"first": "same", "second": "same", "revision": "same", "scope": "same"},
        now=NOW,
    )
    assert features["first"].duplicate_group_size == 2
    assert features["second"].duplicate_group_digest == features["first"].duplicate_group_digest
    assert features["revision"].duplicate_group_size == 1
    assert features["scope"].duplicate_group_size == 1


def test_tokenizer_profile_drifti_fail_closed() -> None:
    candidate = replace(_candidate("candidate"), tokenizer_profile_digest=digest("other"))
    with pytest.raises(PolicyViolation, match="tokenizer profile drift"):
        ContextRankingFeatureBuilder(_request()).build_all(
            (candidate,), {"candidate": "same"}, now=NOW
        )


def test_caller_token_count_ve_ranking_snapshot_drifti_fail_closed() -> None:
    candidate = replace(_candidate("candidate"), token_count=1)
    with pytest.raises(PolicyViolation, match="token count drift"):
        ContextRankingFeatureBuilder(_request()).build_all(
            (candidate,), {"candidate": "same"}, now=NOW
        )
    request = _request()
    snapshot = ContextRankingSnapshotIssuer.issue(
        request=request,
        realm_ref="realm/one",
        project_ref="project/one",
        work_ref="work/one",
        step_ref="step/one",
        assignment_id="00000000-0000-0000-0000-000000000005",
        assignment_digest=digest("assignment"),
        source_snapshot_digest=digest("source-snapshot"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(minutes=5),
    )
    ContextRankingSnapshotIssuer.verify(snapshot, now=NOW)
    forged = replace(
        snapshot,
        request=replace(request, current_source_revision="source/forged"),
    )
    with pytest.raises(PolicyViolation, match="issuance provenance"):
        ContextRankingSnapshotIssuer.verify(forged, now=NOW)
    with pytest.raises(PolicyViolation, match="stale"):
        ContextRankingSnapshotIssuer.verify(snapshot, now=NOW + dt.timedelta(minutes=5))
