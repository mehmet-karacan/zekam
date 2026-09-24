"""AC-02 Context Plane ECONOMY budget modu testleri.

ECONOMY daha dusuk/bounded context budget uretir; fakat authority/policy/scope/redaction
check'leri NORMAL ile ayni kalir (gorev invariant). NORMAL ile ayni candidate set ve
requirement'lar verildiginde ECONOMY guvenlik denetimlerini azaltmaz; yalniz istege bagli
expensive expansion'i (L3 full source/reference/script/asset) L2 kapasitesine sinirlar.
"""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.context_compiler import compile_context_plane_v2
from zekam.application.context_ranking import ContextRankingRequest
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextBudgetMode,
    ContextCandidate,
    ContextCandidateKind,
    ContextLoadLevel,
    ContextManifest,
    ContextSourceKind,
    OmittedReason,
)
from zekam.domain.errors import ValidationFailed

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 9, 24, tzinfo=dt.UTC)


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
    tokens: int = 10,
    required: bool = False,
    authority: AuthorityLevel = AuthorityLevel.VERIFIED,
    source_kind: ContextSourceKind = ContextSourceKind.KNOWLEDGE,
    load_level: ContextLoadLevel = ContextLoadLevel.L2,
) -> ContextCandidate:
    value = candidate_id.ljust(tokens, "x")[:tokens]
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=authority,
        observed_at=NOW,
        source_revision="revision/current",
        content_digest=digest(value),
        token_count=len(value.encode("utf-8")),
        required=required,
        kind=ContextCandidateKind.KNOWLEDGE,
        source_ref=f"context/{candidate_id}",
        identity_refs=("entity/task",),
        scope_ref="work/current",
        applicable_roles=("builder",),
        task_terms=("java",),
        load_level=load_level,
        source_kind=source_kind,
    )


def _compile(
    candidates: tuple[ContextCandidate, ...],
    *,
    budget: int,
    mode: ContextBudgetMode,
    max_load_level: ContextLoadLevel = ContextLoadLevel.L2,
) -> ContextManifest:
    contents = {
        item.candidate_id: item.candidate_id.ljust(item.token_count, "x")[: item.token_count]
        for item in candidates
    }
    return compile_context_plane_v2(
        candidates,
        ranking_request=_request(),
        token_budget=budget,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        recipe_id="context-plane-test",
        recipe_digest=digest("recipe"),
        target_role="builder",
        contents=contents,
        ranking_snapshot_digest=digest("ranking-snapshot"),
        candidate_set_digest=digest("candidate-set"),
        budget_mode=mode,
        max_load_level=max_load_level,
    )


def test_economy_uretilen_context_normalden_daha_bounded_dusuktur() -> None:
    """L3 expensive expansion NORMAL'de acilir, ECONOMY'de L2'ye sinirlanip deferred edilir."""
    l3 = _candidate("l3-source", tokens=80, load_level=ContextLoadLevel.L3)
    normal = _compile(
        (l3,), budget=100, mode=ContextBudgetMode.NORMAL, max_load_level=ContextLoadLevel.L3
    )
    economy = _compile(
        (l3,), budget=100, mode=ContextBudgetMode.ECONOMY, max_load_level=ContextLoadLevel.L3
    )
    assert {s.candidate_id for s in normal.selected} == {"l3-source"}
    assert sum(s.token_count for s in normal.selected) > sum(
        s.token_count for s in economy.selected
    )
    assert {s.candidate_id for s in economy.selected} == set()
    omitted = economy.omitted[0]
    assert omitted.reason is OmittedReason.LOAD_LEVEL


def test_economy_authority_denetimi_normal_ile_aynidir() -> None:
    """Yetersiz authority'li aday her iki modda da ayni nedenle reddedilir."""
    untrusted = _candidate("low", authority=AuthorityLevel.UNTRUSTED)
    for mode in (ContextBudgetMode.NORMAL, ContextBudgetMode.ECONOMY):
        manifest = _compile((untrusted,), budget=100, mode=mode)
        assert {s.candidate_id for s in manifest.selected} == set()
        assert manifest.omitted[0].reason is OmittedReason.INSUFFICIENT_AUTHORITY


def test_economy_scope_denetimi_normal_ile_aynidir() -> None:
    """Scope disi aday her iki modda da ayni nedenle reddedilir."""
    from dataclasses import replace

    external = replace(
        _candidate("external", authority=AuthorityLevel.VERIFIED),
        scope_ref="scope/other-project",
    )
    for mode in (ContextBudgetMode.NORMAL, ContextBudgetMode.ECONOMY):
        manifest = _compile((external,), budget=100, mode=mode)
        assert {s.candidate_id for s in manifest.selected} == set()
        assert manifest.omitted[0].reason is OmittedReason.SCOPE_MISMATCH


def test_economy_boundaries_normal_ile_ayni_kalir() -> None:
    """Budget/timezone gecersiz input her iki modda da ayni ValidationFailed uretir."""
    for mode in (ContextBudgetMode.NORMAL, ContextBudgetMode.ECONOMY):
        with pytest.raises(ValidationFailed, match="budget"):
            _compile((_candidate("a", tokens=5),), budget=0, mode=mode)
        with pytest.raises(ValidationFailed, match="budget"):
            _compile((_candidate("a", tokens=5),), budget=-1, mode=mode)


def test_economy_partition_tekilligi_korunur() -> None:
    """Her aday tam selected/omitted partition'ina yerlestirilir; kayip veya cift yok."""
    candidates = (
        _candidate("work-a", source_kind=ContextSourceKind.WORK),
        _candidate(
            "memo-b",
            source_kind=ContextSourceKind.MEMORY,
            authority=AuthorityLevel.UNTRUSTED,
        ),
        _candidate("l3-c", tokens=50, load_level=ContextLoadLevel.L3),
    )
    manifest = _compile(candidates, budget=20, mode=ContextBudgetMode.ECONOMY)
    selected_ids = {s.candidate_id for s in manifest.selected}
    omitted_ids = {o.candidate_id for o in manifest.omitted}
    assert selected_ids & omitted_ids == set()
    assert selected_ids | omitted_ids == {c.candidate_id for c in candidates}
