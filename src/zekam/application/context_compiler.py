"""Context compiler v2: exact partition, dedupe, explainable score ve metrics."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping

from zekam.application.context_ranking import (
    ContextRankingFeatureBuilder,
    ContextRankingRequest,
)
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextManifest,
    ContextOmission,
    ContextSelection,
    OmittedReason,
)
from zekam.domain.context_scoring import (
    CONTEXT_SCORING_POLICY_DIGEST,
    CONTEXT_SCORING_POLICY_VERSION,
    ContextCompilerMetricsV2,
    ContextRankFeatures,
    ContextScoreV2,
    ContextSelectionReason,
    ScopeProximity,
    SourceRevisionState,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _ppm(numerator: int, denominator: int) -> int:
    return 0 if denominator <= 0 else min(1_000_000, numerator * 1_000_000 // denominator)


def _score(candidate: ContextCandidate, features: ContextRankFeatures) -> ContextScoreV2:
    return ContextScoreV2(
        required=int(candidate.required),
        authority=int(candidate.authority),
        exact_identity=int(features.exact_identity),
        scope_proximity=int(features.scope_proximity),
        source_compatible=int(
            features.source_revision_state
            in {SourceRevisionState.CURRENT, SourceRevisionState.COMPATIBLE}
        ),
        evidence_strength=features.evidence_strength,
        role_relevance=features.role_relevance,
        task_relevance=features.task_relevance,
        freshness_bucket=features.freshness_bucket,
        conflict_penalty=-features.conflict_count,
        duplicate_penalty=-(features.duplicate_group_size - 1),
        token_efficiency=-candidate.token_count,
        candidate_id=candidate.candidate_id,
    )


def _sort_key(score: ContextScoreV2) -> tuple[int | str, ...]:
    values = score.lexicographic
    return tuple(-value if isinstance(value, int) else value for value in values)


def _reason_codes(candidate: ContextCandidate, features: ContextRankFeatures) -> tuple[str, ...]:
    reasons = [ContextSelectionReason.REQUIRED.value] if candidate.required else []
    factors = (
        (int(candidate.authority) > 0, ContextSelectionReason.AUTHORITY),
        (features.exact_identity, ContextSelectionReason.EXACT_IDENTITY),
        (
            features.scope_proximity > ScopeProximity.EXTERNAL,
            ContextSelectionReason.SCOPE_PROXIMITY,
        ),
        (
            features.source_revision_state
            in {SourceRevisionState.CURRENT, SourceRevisionState.COMPATIBLE},
            ContextSelectionReason.SOURCE_COMPATIBLE,
        ),
        (features.evidence_strength > 0, ContextSelectionReason.EVIDENCE_STRENGTH),
        (features.role_relevance > 0, ContextSelectionReason.ROLE_RELEVANCE),
        (features.task_relevance > 0, ContextSelectionReason.TASK_RELEVANCE),
        (features.freshness_bucket > 0, ContextSelectionReason.FRESHNESS),
        (features.conflict_count > 0, ContextSelectionReason.CONFLICT_PENALTY),
        (features.duplicate_group_size > 1, ContextSelectionReason.DUPLICATE_PENALTY),
        (True, ContextSelectionReason.TOKEN_EFFICIENCY),
        (True, ContextSelectionReason.STABLE_ID),
    )
    reasons.extend(reason.value for enabled, reason in factors if enabled)
    return tuple(reasons)


def _feature_rejection(
    candidate: ContextCandidate,
    features: ContextRankFeatures,
    request: ContextRankingRequest,
) -> OmittedReason | None:
    if request.target_identity_refs and candidate.identity_refs and not features.exact_identity:
        return OmittedReason.IDENTITY_MISMATCH
    if request.realm_scope_ref is not None and features.scope_proximity is ScopeProximity.EXTERNAL:
        return OmittedReason.SCOPE_MISMATCH
    if features.source_revision_state is SourceRevisionState.CONFLICT:
        return OmittedReason.CONFLICT
    if features.source_revision_state is SourceRevisionState.MISMATCH:
        return OmittedReason.SOURCE_REVISION_MISMATCH
    if candidate.applicable_roles and features.role_relevance == 0:
        return OmittedReason.ROLE_MISMATCH
    if request.task_terms and candidate.task_terms and features.task_relevance == 0:
        return OmittedReason.LOW_RELEVANCE
    return None


def compile_context_v2(
    candidates: tuple[ContextCandidate, ...],
    *,
    ranking_request: ContextRankingRequest,
    token_budget: int,
    minimum_authority: AuthorityLevel,
    now: dt.datetime,
    recipe_id: str | None = None,
    recipe_digest: str | None = None,
    target_role: str | None = None,
    pre_omitted: tuple[ContextOmission, ...] = (),
    contents: Mapping[str, str],
    ranking_snapshot_digest: str,
    candidate_set_digest: str,
) -> ContextManifest:
    """Her input adayi tam bir selected/omitted partitionina yerlestirir."""

    if token_budget < 1 or now.tzinfo is None:
        raise ValidationFailed("Context compiler v2 budget ve timezone ister")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValidationFailed("Context compiler v2 candidate kimlikleri tekil olmali")
    if target_role is not None and target_role != ranking_request.role:
        raise PolicyViolation("Context ranking request target role drift")
    pre_omitted_ids = {item.candidate_id for item in pre_omitted}
    if len(pre_omitted_ids) != len(pre_omitted):
        raise ValidationFailed("Context pre-omission kimlikleri tekil olmali")
    candidate_by_id = {item.candidate_id: item for item in candidates}
    if not pre_omitted_ids <= set(candidate_by_id):
        raise PolicyViolation("Context pre-omission input partition disinda")
    features = ContextRankingFeatureBuilder(ranking_request).build_all(
        candidates, contents, now=now
    )
    omissions = list(pre_omitted)
    eligible: list[ContextCandidate] = []
    for candidate in candidates:
        if candidate.candidate_id in pre_omitted_ids:
            continue
        rejection = candidate.rejection(now, minimum_authority) or _feature_rejection(
            candidate, features[candidate.candidate_id], ranking_request
        )
        if rejection is not None:
            if candidate.required:
                raise PolicyViolation(
                    f"Required context candidate uygun degil: {candidate.candidate_id}"
                    f" ({rejection.value})"
                )
            omissions.append(
                ContextOmission(candidate.candidate_id, rejection, candidate.token_count)
            )
        else:
            eligible.append(candidate)

    groups: dict[str, list[ContextCandidate]] = {}
    for candidate in eligible:
        group = features[candidate.candidate_id].duplicate_group_digest
        if group is not None:
            groups.setdefault(group, []).append(candidate)
    duplicate_ids: set[str] = set()
    for group_digest, members in groups.items():
        required = [item for item in members if item.required]
        if len(required) > 1:
            raise PolicyViolation("Required duplicate context fail-closed review ister")
        representative = (
            required[0]
            if required
            else sorted(
                members, key=lambda item: _sort_key(_score(item, features[item.candidate_id]))
            )[0]
        )
        for duplicate in members:
            if duplicate.candidate_id == representative.candidate_id:
                continue
            duplicate_ids.add(duplicate.candidate_id)
            omissions.append(
                ContextOmission(
                    duplicate.candidate_id,
                    OmittedReason.DUPLICATE,
                    duplicate.token_count,
                    representative.candidate_id,
                    group_digest,
                )
            )
    ranked = [item for item in eligible if item.candidate_id not in duplicate_ids]
    ranked.sort(key=lambda item: _sort_key(_score(item, features[item.candidate_id])))
    required_tokens = sum(item.token_count for item in ranked if item.required)
    if required_tokens > token_budget:
        raise PolicyViolation("Required context token budget'e sigmiyor")
    selected: list[ContextSelection] = []
    remaining = token_budget
    for candidate in ranked:
        score = _score(candidate, features[candidate.candidate_id])
        if candidate.token_count > remaining:
            if candidate.required:
                raise PolicyViolation("Required context token budget'e sigmiyor")
            omissions.append(
                ContextOmission(candidate.candidate_id, OmittedReason.BUDGET, candidate.token_count)
            )
            continue
        selected.append(
            ContextSelection(
                candidate_id=candidate.candidate_id,
                content_digest=candidate.content_digest,
                token_count=candidate.token_count,
                score=score.lexicographic,
                reason="context-score-v2",
                kind=candidate.kind,
                source_ref=candidate.source_ref,
                source_revision=candidate.source_revision,
                candidate_digest=candidate.candidate_digest,
                authority=candidate.authority,
                reason_codes=_reason_codes(candidate, features[candidate.candidate_id]),
            )
        )
        remaining -= candidate.token_count
    omission_ids = {item.candidate_id for item in omissions}
    selected_ids = {item.candidate_id for item in selected}
    if selected_ids & omission_ids or selected_ids | omission_ids != set(candidate_by_id):
        raise PolicyViolation("Context compiler v2 exact candidate partition drift")
    sorted_omissions = tuple(sorted(omissions, key=lambda item: item.candidate_id))
    input_tokens = sum(item.token_count for item in candidates)
    selected_tokens = sum(item.token_count for item in selected)
    omitted_tokens = sum(item.token_count for item in sorted_omissions)
    duplicate_tokens = sum(
        item.token_count for item in sorted_omissions if item.reason is OmittedReason.DUPLICATE
    )
    eligible_tokens = sum(item.token_count for item in eligible)
    selected_relevance_units = sum(
        (features[item.candidate_id].role_relevance + features[item.candidate_id].task_relevance)
        * item.token_count
        for item in selected
    )
    reason_counts = Counter(item.reason.value for item in sorted_omissions)
    metrics = ContextCompilerMetricsV2(
        input_count=len(candidates),
        input_tokens=input_tokens,
        eligible_count=len(eligible),
        eligible_tokens=eligible_tokens,
        selected_count=len(selected),
        selected_tokens=selected_tokens,
        omitted_count=len(sorted_omissions),
        omitted_tokens=omitted_tokens,
        required_total=sum(item.required for item in candidates),
        required_selected=sum(candidate_by_id[item.candidate_id].required for item in selected),
        duplicate_suppressed_count=reason_counts[OmittedReason.DUPLICATE.value],
        duplicate_suppressed_tokens=duplicate_tokens,
        token_budget=token_budget,
        token_utilization_ppm=_ppm(selected_tokens, token_budget),
        token_efficiency_ppm=_ppm(selected_relevance_units, selected_tokens * 8),
        duplicate_token_ratio_ppm=_ppm(duplicate_tokens, eligible_tokens),
        omission_counts=tuple(sorted(reason_counts.items())),
    )
    fingerprint = digest(
        [item.candidate_digest for item in sorted(candidates, key=lambda row: row.candidate_id)]
    )
    return ContextManifest(
        token_budget=token_budget,
        selected=tuple(selected),
        omitted=sorted_omissions,
        candidate_fingerprint=fingerprint,
        created_at=now,
        recipe_id=recipe_id,
        recipe_digest=recipe_digest,
        target_role=target_role,
        compiler_version=CONTEXT_SCORING_POLICY_VERSION,
        scoring_policy_digest=CONTEXT_SCORING_POLICY_DIGEST,
        compiler_metrics=metrics,
        ranking_snapshot_digest=ranking_snapshot_digest,
        candidate_set_digest=candidate_set_digest,
    )
