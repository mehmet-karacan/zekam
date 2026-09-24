"""AC-01 Context Plane progressive disclosure testleri.

Yeni oturum eager olarak tum memory/knowledge/skill body'lerini yuklemez; once
metadata/index (L0), sonra secilen kaynaklar lazy (L2), L3 (references/scripts/assets/full
source) yalniz explicit need'te acilir. Varsayilan baslangic tum corpus DEGIL.
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
    source_kind: ContextSourceKind = ContextSourceKind.KNOWLEDGE,
    load_level: ContextLoadLevel = ContextLoadLevel.L2,
) -> ContextCandidate:
    value = candidate_id.ljust(tokens, "x")[:tokens]
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision="revision/current",
        content_digest=digest(value),
        token_count=len(value.encode("utf-8")),
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
    mode: ContextBudgetMode = ContextBudgetMode.NORMAL,
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


def test_yeni_sesion_tum_corpusu_eager_yuklemez_l0_metadata_once() -> None:
    """Varsayilan max L2: L3 full-source/adaylar eager yuklenmez, deferred edilir."""
    l3_assets = (
        _candidate("asset-a", tokens=40, load_level=ContextLoadLevel.L3),
        _candidate("asset-b", tokens=40, load_level=ContextLoadLevel.L3),
    )
    manifest = _compile(l3_assets, budget=100)
    # L3 asset'ler default max L2 ile acilmaz.
    assert {s.candidate_id for s in manifest.selected} == set()
    reasons = {o.candidate_id: o.reason for o in manifest.omitted}
    assert reasons == {"asset-a": OmittedReason.LOAD_LEVEL, "asset-b": OmittedReason.LOAD_LEVEL}


def test_secilen_kaynaklar_lazy_acilir_l3_explicit_needte() -> None:
    """max_load_level L3 verildiginde L3 kaynak secilir (explicit need'te lazy acilir)."""
    l3 = _candidate("full-source", tokens=40, load_level=ContextLoadLevel.L3)
    manifest = _compile((l3,), budget=100, max_load_level=ContextLoadLevel.L3)
    assert {s.candidate_id for s in manifest.selected} == {"full-source"}
    selection = manifest.selected[0]
    assert selection.load_level is ContextLoadLevel.L3
    assert selection.source_kind is ContextSourceKind.KNOWLEDGE


def test_l2_selected_body_lazy_ve_disclosure_izlenir() -> None:
    """L2 selected body secilir; disclosure trace authority false tasir (context != authority)."""
    body = _candidate("selected-body", tokens=8, load_level=ContextLoadLevel.L2)
    manifest = _compile((body,), budget=100)
    selection = manifest.selected[0]
    assert selection.load_level is ContextLoadLevel.L2
    assert selection.reason == "context-plane-v2"
    trace = selection.disclosure_trace()
    assert trace["load_level"] == "L2"
    assert trace["budget_mode"] == "normal"
    assert trace["authority"] is False


def test_metadata_her_zaman_acik_fakat_korpus_degil() -> None:
    """L0 metadata index'i secilebilir; default baslangic tum corpus yuklenmez."""
    meta = _candidate("metadata-index", tokens=3, load_level=ContextLoadLevel.L0)
    manifest = _compile((meta,), budget=100)
    assert {s.candidate_id for s in manifest.selected} == {"metadata-index"}
    # L0 metadata secilirken ayni korpusun L3 body'si deferred kalir.
    both = (meta, _candidate("body", tokens=40, load_level=ContextLoadLevel.L3))
    manifest = _compile(both, budget=100)
    assert {s.candidate_id for s in manifest.selected} == {"metadata-index"}
    assert any(o.reason is OmittedReason.LOAD_LEVEL for o in manifest.omitted)
