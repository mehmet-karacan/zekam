from __future__ import annotations

from zekam.application.embedding_routing import (
    EmbeddingRouteCandidate,
    EmbeddingRouteKind,
    select_embedding_route,
)
from zekam.domain.canonical import digest


def _candidate(
    name: str,
    *,
    latency: float,
    margin: float,
    qualified: bool = True,
    fresh: bool = True,
) -> EmbeddingRouteCandidate:
    return EmbeddingRouteCandidate(
        model_ref=name,
        dimension=1024,
        qualified=qualified,
        health_fresh=fresh,
        verified=True,
        latency_ms=latency,
        semantic_margin=margin,
        qualification_evidence_digest=digest({"qualification": name}),
        probe_evidence_digest=digest({"probe": name}),
    )


def test_remote_route_prefers_margin_then_latency_when_explicitly_allowed() -> None:
    decision = select_embedding_route(
        (_candidate("bge", latency=20, margin=0.3), _candidate("qwen", latency=10, margin=0.5)),
        local_model_ref="local/hash-v1",
        local_dimension=1024,
        remote_source_allowed=True,
    )
    assert decision.kind is EmbeddingRouteKind.QUALIFIED_REMOTE
    assert decision.model_ref == "qwen"


def test_remote_candidate_never_receives_project_source_without_policy() -> None:
    decision = select_embedding_route(
        (_candidate("qwen", latency=10, margin=0.5),),
        local_model_ref="local/hash-v1",
        local_dimension=1024,
        remote_source_allowed=False,
    )
    assert decision.kind is EmbeddingRouteKind.LOCAL_FALLBACK
    assert decision.reasons == ("remote-project-source-not-authorized",)


def test_missing_or_stale_candidate_uses_explicit_local_fallback() -> None:
    decision = select_embedding_route(
        (_candidate("qwen", latency=10, margin=0.5, fresh=False),),
        local_model_ref="local/hash-v1",
        local_dimension=1024,
        remote_source_allowed=True,
    )
    assert decision.kind is EmbeddingRouteKind.LOCAL_FALLBACK
    assert decision.reasons == ("qualified-embedding-candidate-missing",)
