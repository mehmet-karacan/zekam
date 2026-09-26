"""Fresh-source embedded project indexing and evidence-gated hybrid retrieval."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from zekam.application.code_graph_ranking import (
    GraphRankingConfig,
    GraphReadPort,
    ProjectGraphReranker,
)
from zekam.application.embedding_provider import EmbeddingPolicy, EmbeddingProvider
from zekam.application.knowledge_index import (
    KnowledgeGeneration,
    KnowledgeIndexPort,
    KnowledgeIndexRecord,
)
from zekam.application.project_knowledge_index import ProjectIndexPlan
from zekam.application.retrieval_service import (
    DEFAULT_RETRIEVAL_DEADLINE_SECONDS,
    QueryIntent,
    Reranker,
    RetrievalService,
    RetrievalTrace,
    _classify_intent,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.retrieval import (
    AnswerState,
    ScoredHit,
    answer_semantics,
    extract_identifiers,
)

_TOKEN = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_BYTES = 16 * 1024
DEFAULT_DENSE_EVIDENCE_THRESHOLD = 0.49
DEFAULT_DENSE_MARGIN_THRESHOLD = 0.04
DEFAULT_LEXICAL_COVERAGE_THRESHOLD = 0.50

#: WP6 (B07): an evidence set is only "relationship-satisfying" when at least one
#: of the gathered chunks carries genuine call-site / data-flow / dependency edge
#: language.  Merely finding A and B separately is NOT an edge (no fabrication).
_RELATIONSHIP_EDGE_SIGNALS = (
    "calls",
    "çağırır",
    "cagirir",
    "çağrı",
    "cagri",
    "call",
    "bağımlı",
    "bagimli",
    "bağımlılık",
    "bagimlilik",
    "dependency",
    "depends on",
    "uses",
    "import ",
    "from ",
    "->",
    "-->",
    "::",
    "calls into",
    "invokes",
)

#: WP6 (B08): maximum length (characters) of the answer excerpt that is taken
#: from a single used chunk.  Kept bounded so the output contract stays small
#: and model context/output reserve is respected.  The excerpt is chosen from
#: the *used* chunks (evidence-selected), not a blind first-500 of the very
#: first chunk.
MAX_ANSWER_EXCERPT_CHARS = 500


def _excerpt_window(text: str, max_chars: int) -> tuple[str, bool]:
    """Return a bounded, line-aligned leading window of ``text`` for the excerpt.

    WP6 (B08): the answer excerpt is an *evidence-selected* bounded window, not a
    blind first-500-character slice of the first used chunk with no provenance.
    The window is line-aligned and deterministic; when the whole chunk fits it is
    returned unchanged.  The returned ``(window, truncated)`` flag tells the
    caller whether the chunk was truncated, so the excerpt's derived digest and
    locator relationship stay accurate.
    """
    if max_chars <= 0:
        return "", text != ""
    if len(text) <= max_chars:
        return text, False
    lines = text.splitlines(keepends=True)
    accumulated: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) > max_chars:
            if not accumulated:
                return text[:max_chars], True
            break
        accumulated.append(line)
        total += len(line)
    return "".join(accumulated), True


def _build_excerpt(answer: Any, views: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Build the answer excerpt window and its additive provenance metadata.

    WP6 (B08): ``answer_excerpt`` stays a **string** (backward-compatible with the
    existing CLI/consumer contract), but is now the bounded, line-aligned window
    of the first *used* chunk — an evidence-selected window rather than a blind
    first-500-character slice.  The accurate digest/locator relationship is
    carried in a separate additive ``answer_excerpt_meta`` field so the excerpt
    digest is never mixed with the full source/chunk digest.

    Returns ``(text_or_None, meta_or_None)``.
    """
    if not answer.used_chunk_ids or not answer.citations:
        return None, None
    chunk_id = answer.used_chunk_ids[0]
    view = views.get(chunk_id)
    if view is None:
        return None, None
    text, truncated = _excerpt_window(view.text, MAX_ANSWER_EXCERPT_CHARS)
    meta: dict[str, Any] = {
        "chunk_id": chunk_id,
        "truncated": truncated,
        # Derived digest of JUST the excerpt window — never confused with the
        # full chunk content_digest (kept distinct and clearly labelled).
        "excerpt_digest": digest_of_bytes(text.encode("utf-8")),
        "content_digest": view.content_digest,
        "locator": view.locator.as_dict() if view.locator else None,
        "source_ref": view.locator.relative_path if view.locator else None,
    }
    return text, meta


def _tokens(value: str) -> frozenset[str]:
    return frozenset(item.casefold() for item in _TOKEN.findall(value) if len(item) > 1)


def _supports_identifier(text: str, identifier: str) -> bool:
    """Require one answerable chunk to contain a single technical identity."""
    text_tokens = _tokens(text)
    return all(
        part.casefold() in text_tokens
        for part in re.findall(r"[A-Za-z0-9_$#]+", identifier)
        if len(part) > 1
    )


def _supports_all_identifiers(text: str, identifiers: tuple[str, ...]) -> bool:
    """Require one answerable chunk to contain every technical query identity.

    This is the *single-object* contract: a single chunk must support the object
    (kept strict for single-object questions).  For multi-object/comparison/
    relationship questions this per-chunk requirement is intentionally NOT used —
    coverage is collective across the evidence set (WP6 / B07).
    """

    if not identifiers:
        return True
    text_tokens = _tokens(text)
    return all(
        all(
            part.casefold() in text_tokens
            for part in re.findall(r"[A-Za-z0-9_$#]+", identifier)
            if len(part) > 1
        )
        for identifier in identifiers
    )


def _collective_identifier_coverage(
    views: dict[str, Any],
    candidate_ids: tuple[str, ...],
    identifiers: tuple[str, ...],
) -> tuple[bool, frozenset[str]]:
    """Return (covered, missing) — whether the evidence SET covers every identifier.

    WP6 (B07): a verified evidence set collectively satisfies a multi-object or
    comparison question when, across all candidate chunks, every required
    identifier appears at least once.  Unlike ``_supports_all_identifiers`` this
    does NOT demand a single chunk to hold every identifier; the objects may live
    in different files/chunks.  Each identifier must be covered at least once so
    none is starved (aligns with B04 fair exact).

    Note this deliberately computes coverage from *candidate* texts regardless of
    whether they were later dropped for the context budget.  The caller applies
    budget filtering separately; evidence-gating (is there enough evidence at all)
    and context-packing (which verified evidence fits the budget) stay distinct.
    """
    if not identifiers:
        return True, frozenset()
    text_tokens: dict[str, frozenset[str]] = {}
    for chunk_id in candidate_ids:
        view = views.get(chunk_id)
        if view is not None:
            text_tokens[chunk_id] = _tokens(view.text)
    covered: set[str] = set()
    for identifier in identifiers:
        parts = [
            part.casefold()
            for part in re.findall(r"[A-Za-z0-9_$#]+", identifier)
            if len(part) > 1
        ]
        if any(all(part in tokens for part in parts) for tokens in text_tokens.values()):
            covered.add(identifier)
    missing = frozenset(identifier for identifier in identifiers if identifier not in covered)
    return not missing, missing


def _relationship_edge_evidence(views: dict[str, Any], candidate_ids: tuple[str, ...]) -> bool:
    """Return True only when at least one candidate chunk carries edge language.

    WP6 (B07) relationship rule: finding A and B separately is NOT an edge.  A
    relationship / call-chain / dependency assertion is only consider satisfied
    when an actual call-site, data-flow or dependency reference exists in the
    gathered source.  This keeps the system from fabricating an edge from mere
    co-presence of names.
    """
    lowered = " ".join(
        views[chunk_id].text.casefold()
        for chunk_id in candidate_ids
        if chunk_id in views
    )
    return any(signal in lowered for signal in _RELATIONSHIP_EDGE_SIGNALS)


def compose_graph_reranker(
    graph: GraphReadPort,
    *,
    project_id: str,
    source_revision: str,
    tree_digest: str,
    chunk_file: Callable[[str], str | None],
    enabled: bool = False,
    config: GraphRankingConfig | None = None,
) -> Reranker | None:
    """Compose an optional graph reranker.

    Default-off (task #17): unless ``enabled`` is true, ``None`` is returned so
    ``EmbeddedProjectRAG.reranker`` stays unchanged and the baseline RAG path is
    preserved byte-for-byte.  All binding identity checks run inside the
    reranker per query; a binding mismatch simply bypasses it at runtime.
    """
    if not enabled:
        return None
    try:
        return ProjectGraphReranker(
            graph,
            project_id=project_id,
            source_revision=source_revision,
            tree_digest=tree_digest,
            chunk_file=chunk_file,
            config=config,
        )
    except Exception:
        return None


def build_embedded_project_generation(
    index: KnowledgeIndexPort,
    plan: ProjectIndexPlan,
    *,
    embedding_provider: EmbeddingProvider,
    embedding_policy: EmbeddingPolicy,
    created_at: dt.datetime | None = None,
) -> tuple[ProjectIndexPlan, KnowledgeGeneration]:
    """Embed every bounded source chunk before atomically activating the generation."""

    profile = embedding_provider.describe()
    profile.assert_policy(embedding_policy)
    if (
        plan.embedding_profile.dimension != profile.dimension
        or plan.embedding_profile.model_ref
        not in {profile.exact_model_id, f"openai/{profile.exact_model_id}"}
    ):
        raise PolicyViolation("Embedded project plan/provider drift")
    bound_plan = replace(
        plan,
        embedding_profile=replace(
            plan.embedding_profile,
            provider_profile_digest=profile.profile_digest,
        ),
    )
    source_digests = {
        item.relative_path: item.content_digest for item in bound_plan.discovery.files
    }
    vectors: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(bound_plan.chunks), 8):
        chunks = bound_plan.chunks[offset : offset + 8]
        batch = embedding_provider.embed_documents(
            tuple(chunk.text for chunk in chunks), embedding_policy
        )
        if (
            len(batch.vectors) != len(chunks)
            or batch.receipt.vector_count != len(chunks)
            or batch.receipt.profile_digest != profile.profile_digest
            or batch.receipt.dimension != profile.dimension
        ):
            raise PolicyViolation("Embedded project provider partial/receipt drift")
        for chunk, vector in zip(chunks, batch.vectors, strict=True):
            profile.validate_vector(vector)
            vectors[chunk.chunk_id] = vector
    if len(vectors) != len(bound_plan.chunks):
        raise PolicyViolation("Embedded project vector set incomplete")
    records = tuple(
        KnowledgeIndexRecord(
            chunk_id=chunk.chunk_id,
            project_id=str(bound_plan.project_id),
            source_revision=bound_plan.source_revision,
            source_path=str(chunk.locator.relative_path),
            source_digest=source_digests[str(chunk.locator.relative_path)],
            locator=chunk.locator,
            text=chunk.text,
            content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
            chunk_order=chunk.order,
            vector=vectors[chunk.chunk_id],
        )
        for chunk in bound_plan.chunks
    )
    moment = created_at or dt.datetime.now(dt.UTC)
    generation = index.build_generation(
        records,
        project_id=str(bound_plan.project_id),
        source_revision=bound_plan.source_revision,
        tree_digest=bound_plan.tree_digest,
        source_manifest_digest=digest_of_bytes(bound_plan.manifest),
        embedding_profile_digest=bound_plan.embedding_profile.profile_digest,
        provider_profile_digest=profile.profile_digest,
        created_at=moment.isoformat().replace("+00:00", "Z"),
    )
    return bound_plan, generation


@dataclass(slots=True)
class EmbeddedProjectSearchBackend:
    index: KnowledgeIndexPort
    project_id: str
    generation_digest: str
    embedding_provider: EmbeddingProvider
    embedding_policy: EmbeddingPolicy
    source_type: str = "embedded-project-knowledge"
    dense_enabled: bool = True
    dense_failure_reason: str | None = None
    last_exact: tuple[ScoredHit, ...] = ()
    last_lexical: tuple[ScoredHit, ...] = ()
    last_dense: tuple[ScoredHit, ...] = ()

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        self.last_exact = self.index.exact(
            self.project_id,
            identifiers,
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_exact

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        self.last_lexical = self.index.lexical(
            self.project_id,
            query,
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_lexical

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        if not self.dense_enabled:
            self.last_dense = ()
            return ()
        try:
            profile = self.embedding_provider.describe()
            profile.assert_policy(self.embedding_policy)
            batch = self.embedding_provider.embed_query(query, self.embedding_policy)
            if (
                len(batch.vectors) != 1
                or batch.receipt.vector_count != 1
                or batch.receipt.profile_digest != profile.profile_digest
                or batch.receipt.dimension != profile.dimension
            ):
                raise PolicyViolation("Embedded query provider exact tek vector ister")
            profile.validate_vector(batch.vectors[0])
        except Exception as exc:
            self.dense_failure_reason = f"query-embedding-failed:{type(exc).__name__}"
            self.last_dense = ()
            return ()
        self.last_dense = self.index.dense(
            self.project_id,
            batch.vectors[0],
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_dense


@dataclass(frozen=True, slots=True)
class EmbeddedProjectRAG:
    index: KnowledgeIndexPort
    embedding_provider: EmbeddingProvider
    embedding_policy: EmbeddingPolicy
    reranker: Reranker | None = None
    dense_evidence_threshold: float = DEFAULT_DENSE_EVIDENCE_THRESHOLD
    dense_margin_threshold: float = DEFAULT_DENSE_MARGIN_THRESHOLD
    lexical_coverage_threshold: float = DEFAULT_LEXICAL_COVERAGE_THRESHOLD
    # WP5 (P0): one monotonic end-to-end retrieval deadline shared across the
    # discovery/qualification, channels, reranker and fallback; ``None`` disables.
    retrieval_deadline_seconds: float | None = DEFAULT_RETRIEVAL_DEADLINE_SECONDS

    def _stale_result(self, query: str, *, project_id: str, reason: str) -> dict[str, Any]:
        state_value = "abstained-index-unavailable"
        value = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": state_value,
            "project_id": project_id,
            "query_digest": digest({"query": query}),
            "reason": reason,
            "citations": [],
            "searched_channels": [],
            "fallback_allowed": False,
        }
        # WP7 / B08: the embedded contract carries the explicit retrieval-only
        # semantics on every result, including fail-closed stale/index results.
        value["evidence_found"] = False
        value.update(answer_semantics(state_value, evidence_found=False))
        value["retrieval_digest"] = digest(value)
        return value

    def query(
        self,
        query: str,
        *,
        project_id: str,
        expected_source_revision: str,
        expected_tree_digest: str,
        token_budget: int = 1200,
        dense_disabled_reason: str | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > MAX_QUERY_BYTES
        ):
            raise ValidationFailed("RAG query bounded non-empty text olmali")
        try:
            generation = self.index.generation(project_id)
        except ValidationFailed:
            return self._stale_result(query, project_id=project_id, reason="generation-missing")
        if generation.state != "ready":
            return self._stale_result(query, project_id=project_id, reason="generation-not-ready")
        if generation.source_revision != expected_source_revision:
            raise PolicyViolation("RAG generation source revision binding drift")
        if generation.tree_digest != expected_tree_digest:
            raise PolicyViolation("RAG generation source tree binding drift")
        stale_reasons: list[str] = []
        provider_available = False
        provider_failure_reason = dense_disabled_reason or "provider-unavailable"
        if generation.provider_profile_digest != self.embedding_policy.expected_profile_digest:
            provider_failure_reason = "provider-profile-stale"
            stale_reasons.append("embedding-profile-stale")
        elif dense_disabled_reason is None:
            try:
                profile = self.embedding_provider.describe()
                if profile.profile_digest != generation.provider_profile_digest:
                    provider_failure_reason = "provider-profile-stale"
                    stale_reasons.append("embedding-profile-stale")
                else:
                    profile.assert_policy(self.embedding_policy)
                    health = self.embedding_provider.health()
                    provider_available = (
                        health.healthy and health.profile_digest == profile.profile_digest
                    )
                    if not provider_available:
                        provider_failure_reason = "provider-health-unavailable"
            except Exception as exc:
                provider_failure_reason = f"provider-probe-failed:{type(exc).__name__}"

        backend = EmbeddedProjectSearchBackend(
            self.index,
            project_id,
            generation.generation_digest,
            self.embedding_provider,
            self.embedding_policy,
            dense_enabled=provider_available,
            dense_failure_reason=None if provider_available else provider_failure_reason,
        )
        service = RetrievalService(
            backend,
            reranker=self.reranker,
            retrieval_deadline_seconds=self.retrieval_deadline_seconds,
        )
        hits, trace = service.search(query)
        candidate_ids = tuple(hit.chunk_id for hit in hits)
        views = self.index.views(
            project_id,
            candidate_ids,
            generation_digest=generation.generation_digest,
        )
        query_terms = _tokens(query)
        identifiers = extract_identifiers(query)
        lexical_coverage = max(
            (
                len(query_terms & _tokens(views[hit.chunk_id].text)) / len(query_terms)
                for hit in backend.last_lexical
                if query_terms and hit.chunk_id in views
            ),
            default=0.0,
        )
        top_dense = backend.last_dense[0].raw_score if backend.last_dense else -1.0
        dense_margin = (
            top_dense - backend.last_dense[1].raw_score if len(backend.last_dense) > 1 else 0.0
        )
        # WP6 (B07): split the evidence contract by query intent instead of a
        # blanket "one chunk must hold every identifier" requirement.
        #   * single-object (EXACT_LOOKUP or one identifier): a single chunk must
        #     genuinely support the object (strict verification preserved).
        #   * multi-object / comparison (MULTI_OBJECT_COMPARISON or >=2 identifiers):
        #     the verified evidence SET collectively covers all required objects
        #     across chunks; no single-chunk-everything demand.
        #   * relationship (RELATIONSHIP): the set must cover the objects AND an
        #     actual call-site / dependency / data-flow edge must exist — mere
        #     co-presence of A and B is NOT evidence of a relationship (no edge
        #     fabrication).
        intent = _classify_intent(query, identifiers)
        single_object = (
            intent.value == QueryIntent.EXACT_LOOKUP.value
            or (
                # A single-identifier relationship/comparison still targets one
                # object (e.g. "X hangi fonksiyonu cagirir") — strict object
                # support applies, the edge rule is separate.
                len(identifiers) <= 1
            )
        )
        # ``single_chunk_identifier_support`` stays a strict, honest report of
        # whether ANY single chunk holds EVERY identifier (the pre-WP6 contract).
        # It is intentionally NOT relaxed for multi-object questions: it remains
        # a diagnostic even though the *gate* uses the collective contract below.
        single_chunk_identifier_support = any(
            hit.chunk_id in views
            and _supports_all_identifiers(views[hit.chunk_id].text, identifiers)
            for hit in (*backend.last_exact, *backend.last_lexical, *backend.last_dense[:2])
        )
        if single_object:
            coverage_missing: frozenset[str] = frozenset()

            def identity_support_for(text: str) -> bool:
                return _supports_all_identifiers(text, identifiers)
        else:
            # Multi-object: the hits already represent verified candidates.  We
            # require that the evidence SET collectively covers every identifier.
            _, coverage_missing = _collective_identifier_coverage(
                views, candidate_ids, identifiers
            )

            def identity_support_for(text: str) -> bool:
                return True

        exact_identity_support = any(
            hit.chunk_id in views
            and identity_support_for(views[hit.chunk_id].text)
            for hit in backend.last_exact
        )
        lexical_identity_support = any(
            hit.chunk_id in views
            and identity_support_for(views[hit.chunk_id].text)
            for hit in backend.last_lexical
        )
        dense_identity_support = any(
            hit.chunk_id in views
            and identity_support_for(views[hit.chunk_id].text)
            for hit in backend.last_dense[:2]
        )
        enough_evidence = (
            exact_identity_support
            or (lexical_coverage >= self.lexical_coverage_threshold and lexical_identity_support)
            or (
                top_dense >= self.dense_evidence_threshold
                and dense_identity_support
                and (
                    dense_margin >= self.dense_margin_threshold
                    or (
                        lexical_coverage >= self.lexical_coverage_threshold / 2
                        and lexical_identity_support
                    )
                )
            )
        )
        # Multi-object coverage must not starve any required identifier (B04 fair
        # exact).  When an identifier is completely absent from the evidence set,
        # there is not enough evidence to answer the multi-object question.
        if not single_object and coverage_missing:
            enough_evidence = False
        # WP6 (B07) relationship rule: finding names A and B separately is not an
        # edge.  For relationship/call-chain/dependency questions, enough_evidence
        # additionally requires genuine edge language in the gathered source;
        # otherwise the system abstains rather than fabricating a relationship.
        if (
            intent.value == QueryIntent.RELATIONSHIP.value
            and identifiers
            and not _relationship_edge_evidence(views, candidate_ids)
        ):
            enough_evidence = False
            if identifiers and not coverage_missing:
                # Names exist but no edge: explicit no-edge abstain state (never a
                # confidently fabricated relationship answer).
                relationship_state = "abstained-no-edge"
            else:
                relationship_state = None
        else:
            relationship_state = None

        if identifiers and single_object:
            # Single-object strictness: only hits whose single chunk supports the
            # object are retained (preserved, not loosened).
            hits = tuple(
                hit
                for hit in hits
                if hit.chunk_id in views
                and _supports_all_identifiers(views[hit.chunk_id].text, identifiers)
            )
        elif identifiers:
            # Multi/relationship: no per-chunk-everything filter.  Keep hits that
            # exist in the verified views; the collective set already proved
            # coverage (deduped/counted in ``enough_evidence``/coverage_missing).
            hits = tuple(hit for hit in hits if hit.chunk_id in views)
        if not enough_evidence:
            hits = ()
        answer = service.build_answer(
            query,
            hits,
            RetrievalTrace(
                identifiers=extract_identifiers(query),
                per_channel=trace.per_channel,
                fused_count=trace.fused_count,
                after_dedupe=trace.after_dedupe,
                reranker_used=trace.reranker_used,
                reranker_failed=trace.reranker_failed,
                source_type=trace.source_type,
                graph_used=trace.graph_used,
                graph_state=trace.graph_state,
                graph_bypass=trace.graph_bypass,
                intent=trace.intent,
                deadline_expired=trace.deadline_expired,
                degraded_reason=trace.degraded_reason,
            ),
            views=views,
            token_budget=token_budget,
        )
        citations: list[dict[str, Any]] = []
        if answer.citations:
            # WP4 (B04): bulk-hydrate the identity fields for every cited chunk
            # in ONE bounded SQL read instead of one source_identity round-trip
            # per citation (the N+1 pattern).  Same validation and output shape
            # as the previous per-citation path; only the final citation chunk
            # set is read.  source_identity is kept for any other callers.
            citation_ids = tuple(citation.chunk_id for citation in answer.citations)
            identities = self.index.source_identities(
                project_id,
                citation_ids,
                generation_digest=generation.generation_digest,
            )
        else:
            identities = {}
        for citation in answer.citations:
            identity = identities[citation.chunk_id]
            view = views[citation.chunk_id]
            fused_hit = next(hit for hit in hits if hit.chunk_id == citation.chunk_id)
            fused_rank = next(
                rank for rank, hit in enumerate(hits, start=1) if hit.chunk_id == citation.chunk_id
            )
            channel_ranks = {
                channel.value: next(
                    item.rank
                    for item in (
                        *backend.last_exact,
                        *backend.last_lexical,
                        *backend.last_dense,
                    )
                    if item.chunk_id == citation.chunk_id and item.channel is channel
                )
                for channel in fused_hit.channels
            }
            citations.append(
                {
                    "source_id": identity["source_id"],
                    "project_scope": project_id,
                    "source_ref": identity["source_ref"],
                    "source_revision": identity["source_revision"],
                    "source_digest": identity["source_digest"],
                    "content_digest": identity["content_digest"],
                    "chunk_id": citation.chunk_id,
                    "locator_type": (
                        "database-object" if view.locator.object_name else "project-file"
                    ),
                    "locator": view.locator.as_dict(),
                    "retrieval_channels": [item.value for item in fused_hit.channels],
                    "rank_trace": {
                        "fused_rank": fused_rank,
                        "rrf_score": fused_hit.score,
                        "exact_match": fused_hit.exact_match,
                        "channel_ranks": channel_ranks,
                    },
                }
            )
        state = answer.state.value
        # WP6 (B07): explicit no-edge abstain for relationship questions where
        # names exist but no call-site/dependency evidence was found.  This
        # overrides even a generic answered state so the system never fabricates
        # a relationship assertion from mere co-presence of A and B.
        if relationship_state:
            state = relationship_state
            citations = []
        if not enough_evidence and state == AnswerState.ABSTAINED_NO_HIT.value:
            state = AnswerState.ABSTAINED_LOW_EVIDENCE.value
        if backend.dense_failure_reason:
            # WP5 (P0): provider/dependency unavailable.  Strong exact/lexical
            # evidence gathered before the failure may still be returned, but it
            # is marked explicitly degraded ("lexical-only-degraded") rather than
            # a silent full success.  Without strong evidence the result
            # explicitly abstains (never a fabricated "answered").
            if state in (
                AnswerState.ANSWERED.value,
                AnswerState.DEGRADED_PROVIDER_UNAVAILABLE.value,
            ) and (
                exact_identity_support
                or (
                    lexical_coverage >= self.lexical_coverage_threshold
                    and lexical_identity_support
                )
            ):
                state = "lexical-only-degraded"
            else:
                state = AnswerState.ABSTAINED_LOW_EVIDENCE.value
                citations = []
        excerpt, excerpt_meta = _build_excerpt(answer, views)
        # WP7 / B08: `evidence_found` is derived from actual citations carried by
        # the result (never from the `state` string alone) so the retrieval
        # outcome and the payload reality stay consistent.
        evidence_found = bool(citations)
        state_value = state
        result: dict[str, Any] = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": state_value,
            "project_id": project_id,
            "query_digest": answer.query_digest,
            "generation_digest": generation.generation_digest,
            "source_revision": generation.source_revision,
            "tree_digest": generation.tree_digest,
            "embedding_profile_digest": generation.embedding_profile_digest,
            "provider_profile_digest": generation.provider_profile_digest,
            "searched_channels": (
                ["exact", "lexical", "dense"] if provider_available else ["exact", "lexical"]
            ),
            "channel_counts": trace.per_channel,
            "graph_reranker": (
                {
                    "used": trace.graph_used,
                    "state": trace.graph_state,
                    "bypass": trace.graph_bypass,
                }
                if trace.graph_used
                else None
            ),
            "candidate_count": len(candidate_ids),
            "lexical_coverage": lexical_coverage,
            "identifier_count": len(identifiers),
            "query_intent": intent.value,
            "uncovered_identifiers": list(coverage_missing),
            "relationship_state": relationship_state,
            "single_chunk_identifier_support": single_chunk_identifier_support,
            "top_dense_similarity": top_dense,
            "dense_top_2_margin": dense_margin,
            "evidence_sufficient": enough_evidence,
            "degraded_reason": backend.dense_failure_reason,
            "index_freshness": "stale" if stale_reasons else "current",
            "stale_reasons": stale_reasons,
            "snapshot_only": bool(stale_reasons),
            "reindex_recommended": bool(stale_reasons),
            "citations": citations,
            "used_chunk_ids": list(answer.used_chunk_ids),
            "tokens_used": answer.tokens_used,
            "fallback_allowed": False,
            "answer_excerpt": excerpt,
            "answer_excerpt_meta": excerpt_meta,
            "explanation": list(answer.explanation),
        }
        # WP7 / B08: additive answer-semantics fields.  The legacy ``state`` and
        # ``answer_excerpt`` keys above are preserved byte-for-byte for v1
        # consumers; these NEW fields make explicit that this is a RETRIEVAL-ONLY
        # evidence packet and that NO generated natural-language answer exists in
        # this core route.  ``generation_state`` is always ``not_generated`` and
        # ``answer_kind`` is never ``generated_answer`` here (provider-less and
        # unauthorized states included) — no fabricated synthesis is ever claimed.
        result["evidence_found"] = evidence_found
        result.update(answer_semantics(state_value, evidence_found=evidence_found))
        result["retrieval_digest"] = digest(result)
        return result
